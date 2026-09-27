"""Pick Five graph service: FastAPI over HelixDB.

Endpoints (contracts frozen for Slice 3, see README.md):
  GET  /health                      -> {"status": "ok", "title_count": 8800}
  POST /contenders  {taste_vector}   -> ["movie:tmdb:123", ...]  (bare JSON array)
  POST /next-five   {taste_vector}   -> ["movie:tmdb:123", ...]  (5 ids)
  GET  /search?name=Dune&kind=title  -> [{node...}]  (helper for checks / Slice 3)

taste_vector v1:
  {"genres": {"Science Fiction": 1.0},      # genre name -> weight
   "people": {"person:tmdb:114": 0.8},     # person gid  -> weight
   "titles": {"movie:tmdb:969681": 1.0},    # liked title gid -> weight
   "exclude": ["movie:tmdb:111"],           # title gids to skip
   "limit": 20}
All keys optional. Bodies may also be {"taste_vector": {...}, "limit": N}.
"""

from __future__ import annotations

import os
import threading

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from hq import (HAS_GENRE, GENRE_LABEL, PERSON_EDGE_LABELS, PERSON_LABEL,
                TITLE_LABEL, TITLE_PROPS, Helix)
from loader import load_catalog

HELIX_URL = os.environ.get("HELIX_URL", "http://127.0.0.1:8080")
GRAPH_PATH = os.environ.get("GRAPH_PATH", "/srv/graph.jsonl")
MANIFEST_PATH = os.environ.get("MANIFEST_PATH", "/srv/manifest.json")
LIKE_BONUS = 0.5          # per liked-title person overlap
DISCOVERED_PEOPLE_CAP = 25

hx = Helix(HELIX_URL)
GENRE_CACHE: dict[str, int] = {}   # genre name -> HelixDB id

_status = "starting"   # starting|loading|ok|failed
_status_lock = threading.Lock()
_boot_error: str | None = None


def _set_status(s: str) -> None:
    global _status
    with _status_lock:
        _status = s
    print(f"app: status -> {s}", flush=True)


def _get_status() -> str:
    with _status_lock:
        return _status


def _boot() -> None:
    """Background catalog load; flips status to ok/failed when done."""
    global _boot_error
    _set_status("loading")
    try:
        load_catalog(HELIX_URL, GRAPH_PATH, MANIFEST_PATH)
    except Exception as exc:
        _boot_error = f"{type(exc).__name__}: {exc}"[:500]
        print(f"app: catalog load FAILED: {_boot_error}", flush=True)
        _set_status("failed")
        return
    try:
        for row in hx.all_genres():
            GENRE_CACHE[row["name"]] = int(row["$id"])
        print(f"app: cached {len(GENRE_CACHE)} genres", flush=True)
    except Exception as exc:
        print(f"app: genre cache warm failed: {exc}", flush=True)
    _set_status("ok")


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_boot, daemon=True, name="catalog-loader").start()
    yield


app = FastAPI(title="pickfive-helix", lifespan=lifespan)


def _split_body(body: dict) -> tuple[dict, int | None]:
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    tv = body.get("taste_vector", body)
    if not isinstance(tv, dict):
        raise HTTPException(400, "taste_vector must be a JSON object")
    limit = body.get("limit", tv.get("limit"))
    return tv, limit


def _resolve_gids(label: str, gids: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for gid in gids:
        rows = hx.find_by_prop(label, "gid", gid, ["$id", "gid"])
        if rows:
            out[gid] = int(rows[0]["$id"])
    return out


def _score(tv: dict, limit: int) -> list[str]:
    genres: dict[str, float] = tv.get("genres", {}) or {}
    people: dict[str, float] = tv.get("people", {}) or {}
    liked: dict[str, float] = tv.get("titles", {}) or {}
    exclude: set[str] = set(tv.get("exclude", []) or {})

    genre_ids = {n: GENRE_CACHE[n] for n in genres if n in GENRE_CACHE}
    person_ids = _resolve_gids(PERSON_LABEL, list(people))
    liked_ids = _resolve_gids(TITLE_LABEL, list(liked))

    scores: dict[str, float] = {}

    def add(gid: str, w: float) -> None:
        if gid in exclude or gid in liked:
            return
        scores[gid] = scores.get(gid, 0.0) + w

    # genre -> titles
    for name, hid in genre_ids.items():
        for row in hx.titles_in_from([hid], HAS_GENRE):
            add(row["gid"], float(genres[name]))
    # person -> titles (all person edge labels)
    for gid, hid in person_ids.items():
        for label in PERSON_EDGE_LABELS:
            for row in hx.titles_in_from([hid], label):
                add(row["gid"], float(people[gid]))
    # liked titles -> their people -> more titles ("more like this")
    if liked_ids:
        people_of = hx.people_of_titles(list(liked_ids.values()))
        discovered: dict[int, float] = {}
        for tgid, thid in liked_ids.items():
            for prow in people_of.get(thid, []):
                pid = int(prow["$id"])
                if pid in person_ids.values():
                    continue
                discovered[pid] = discovered.get(pid, 0.0) + float(liked[tgid])
        for pid in list(discovered)[:DISCOVERED_PEOPLE_CAP]:
            for label in PERSON_EDGE_LABELS:
                for row in hx.titles_in_from([pid], label):
                    add(row["gid"], LIKE_BONUS * discovered[pid])

    if not scores:
        # no signal: fall back to most popular titles
        rows = hx.all_titles_light()
        rows.sort(key=lambda r: (r.get("popularity") or 0), reverse=True)
        return [r["gid"] for r in rows
                if r["gid"] not in exclude and r["gid"] not in liked][:limit]

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [gid for gid, _ in ranked[:limit]]


@app.get("/health")
def health():
    try:
        n = hx.count_nodes(TITLE_LABEL)
    except Exception:
        return JSONResponse({"status": "degraded", "title_count": 0},
                            status_code=503)
    st = _get_status()
    if st == "failed":
        body: dict = {"status": "failed", "title_count": n}
        if _boot_error:
            body["detail"] = _boot_error
        return JSONResponse(body, status_code=503)
    return {"status": st, "title_count": n}


def _require_ready() -> None:
    st = _get_status()
    if st != "ok":
        raise HTTPException(503, f"catalog {st}, try again shortly")


@app.post("/contenders")
def contenders(body: dict):
    _require_ready()
    tv, limit = _split_body(body)
    try:
        return _score(tv, int(limit) if limit else 20)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"helix query failed: {exc}")


@app.post("/next-five")
def next_five(body: dict):
    _require_ready()
    tv, limit = _split_body(body)
    try:
        return _score(tv, int(limit) if limit else 5)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"helix query failed: {exc}")


@app.get("/search")
def search(name: str = Query(...), kind: str = Query("title")):
    _require_ready()
    label = TITLE_LABEL if kind == "title" else PERSON_LABEL
    props = TITLE_PROPS if kind == "title" else ["$id", "gid", "name", "role"]
    try:
        key = "title" if kind == "title" else "name"
        return hx.find_by_prop(label, key, name, props)
    except Exception as exc:
        raise HTTPException(502, f"helix query failed: {exc}")
