#!/usr/bin/env python3
"""Idempotent catalog loader: graph.jsonl -> HelixDB.

Safe to re-run: if node/edge counts already match manifest.json exactly the
load is skipped. If the database is empty or partially loaded it is reset
(all three labels dropped) and loaded fresh, then verified. Never invents
data; every record comes from graph.jsonl.

Usable as a module (load_catalog) or CLI:
  loader.py --helix-url http://127.0.0.1:8080 \
            --graph /srv/graph.jsonl --manifest /srv/manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

from hq import (HAS_GENRE, GENRE_LABEL, PERSON_EDGE_LABELS, PERSON_LABEL,
                TITLE_LABEL, Helix)

NODE_BATCH = 250
EDGE_BATCH = 250

TITLE_PROP_KEYS = ("title", "year", "type", "description", "image_url",
                   "popularity", "vote_average", "vote_count")


def wait_for_server(base_url: str, timeout_s: int = 120) -> None:
    deadline = time.time() + timeout_s
    url = base_url.rstrip("/") + "/healthz"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    print(f"loader: helix ready at {base_url}", flush=True)
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"loader: helix not ready at {base_url} after {timeout_s}s")


def current_counts(hx: Helix) -> dict:
    counts = {
        "titles": hx.count_nodes(TITLE_LABEL),
        "people": hx.count_nodes(PERSON_LABEL),
        "genres": hx.count_nodes(GENRE_LABEL),
        "edges": 0,
    }
    counts["nodes"] = counts["titles"] + counts["people"] + counts["genres"]
    for label in PERSON_EDGE_LABELS + (HAS_GENRE,):
        counts[label] = hx.count_edges(label)
        counts["edges"] += counts[label]
    return counts


def load_catalog(helix_url: str, graph_path: str, manifest_path: str) -> dict:
    """Run the idempotent load. Returns the verified final counts.

    Raises RuntimeError when the final counts do not match the manifest.
    """
    manifest = json.load(open(manifest_path))
    exp = manifest["graph"]
    exp_nodes, exp_edges = exp["nodes"], exp["edges"]

    wait_for_server(helix_url)
    hx = Helix(helix_url)

    counts = current_counts(hx)
    print(f"loader: current counts {counts}", flush=True)
    if counts["nodes"] == exp_nodes and counts["edges"] == exp_edges:
        print("loader: counts match manifest exactly, skipping load", flush=True)
        return counts

    if counts["nodes"] > 0 or counts["edges"] > 0:
        print("loader: partial state detected, resetting all labels", flush=True)
        for label in (TITLE_LABEL, PERSON_LABEL, GENRE_LABEL):
            hx.drop_label(label)

    # ---- pass 1: nodes -------------------------------------------------
    gid_to_hid: dict[str, int] = {}
    batch: list[tuple[str, dict]] = []

    def flush_nodes() -> None:
        if not batch:
            return
        by_label: dict[str, list[dict]] = {}
        order: list[tuple[str, str]] = []
        for label, props in batch:
            by_label.setdefault(label, []).append(props)
            order.append((label, props["gid"]))
        per_label_ids: dict[str, list[int]] = {}
        offset: dict[str, int] = {}
        for label, props_list in by_label.items():
            per_label_ids[label] = hx.add_nodes(label, props_list)
            offset[label] = 0
        for label, gid in order:
            gid_to_hid[gid] = per_label_ids[label][offset[label]]
            offset[label] += 1
        batch.clear()

    t0 = time.time()
    with open(graph_path) as f:
        for line in f:
            d = json.loads(line)
            if d["kind"] != "node":
                continue
            nt = d["node_type"]
            props = {"gid": d["id"]}
            if nt == "title":
                for k in TITLE_PROP_KEYS:
                    props[k] = d["props"].get(k)
                batch.append((TITLE_LABEL, props))
            elif nt == "person":
                props["name"] = d["props"].get("name")
                props["role"] = d["props"].get("role")
                batch.append((PERSON_LABEL, props))
            elif nt == "genre":
                props["name"] = d["props"].get("name")
                batch.append((GENRE_LABEL, props))
            if len(batch) >= NODE_BATCH:
                flush_nodes()
    flush_nodes()
    print(f"loader: {len(gid_to_hid)} nodes in {time.time()-t0:.1f}s", flush=True)

    # ---- pass 2: edges ---------------------------------------------------
    ebatch: list[tuple[int, str, int]] = []
    n_edges = 0
    t0 = time.time()

    def flush_edges() -> None:
        nonlocal n_edges
        if not ebatch:
            return
        hx.add_edges(ebatch)
        n_edges += len(ebatch)
        ebatch.clear()

    with open(graph_path) as f:
        for line in f:
            d = json.loads(line)
            if d["kind"] != "edge":
                continue
            frm = gid_to_hid.get(d["from"])
            to = gid_to_hid.get(d["to"])
            if frm is None or to is None:
                print(f"loader: WARNING edge references unknown node: {line[:120]}",
                      flush=True)
                continue
            ebatch.append((frm, d["edge_type"], to))
            if len(ebatch) >= EDGE_BATCH:
                flush_edges()
    flush_edges()
    print(f"loader: {n_edges} edges in {time.time()-t0:.1f}s", flush=True)

    # ---- verify ------------------------------------------------------------
    counts = current_counts(hx)
    print(f"loader: final {counts} expected nodes={exp_nodes} edges={exp_edges}",
          flush=True)
    if counts["nodes"] != exp_nodes or counts["edges"] != exp_edges:
        raise RuntimeError(f"loader: COUNT MISMATCH {counts}")
    print("loader: LOAD OK", flush=True)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--helix-url", default="http://127.0.0.1:8080")
    ap.add_argument("--graph", required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    try:
        load_catalog(args.helix_url, args.graph, args.manifest)
        return 0
    except Exception as exc:
        print(f"loader: FAILED: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
