"""Shared HelixDB query layer for the Pick Five graph service.

All queries are built with the official helixdb Python SDK (operation-tree
JSON sent to POST /v2/query). Nothing here invents data: every node and edge
comes from catalog/graph.jsonl.

Graph model (labels):
  Title  {gid, title, year, type, description, image_url, popularity, vote_average, vote_count}
  Person {gid, name, role}
  Genre  {gid, name}
Edges (title -> person/genre):
  directed_by, created_by, authored_by, has_genre
"""

from __future__ import annotations

import time

from helixdb import Client, NodeId, Predicate, g, read_batch, write_batch

TITLE_LABEL = "Title"
PERSON_LABEL = "Person"
GENRE_LABEL = "Genre"
PERSON_EDGE_LABELS = ("directed_by", "created_by", "authored_by")
HAS_GENRE = "has_genre"

TITLE_PROPS = ["$id", "gid", "title", "year", "type", "description",
               "image_url", "popularity", "vote_average", "vote_count"]


class Helix:
    def __init__(self, base_url: str):
        self.client = Client(base_url)

    def _send(self, batch, retries: int = 3):
        last = None
        for attempt in range(retries):
            try:
                return self.client.query(batch.to_query_request())
            except Exception as exc:  # server still starting, etc.
                last = exc
                time.sleep(1.0 * (attempt + 1))
        raise last

    # ---- counts ---------------------------------------------------------
    def count_nodes(self, label: str) -> int:
        rb = read_batch().var_as(
            "c", g().n_with_label(label).count()).returning(["c"])
        return int(self._send(rb)["c"])

    def count_edges(self, label: str) -> int:
        rb = read_batch().var_as(
            "c", g().e_with_label(label).count()).returning(["c"])
        return int(self._send(rb)["c"])

    # ---- writes ---------------------------------------------------------
    def add_nodes(self, label: str, props_list: list[dict]) -> list[int]:
        """Insert nodes in one write batch; returns HelixDB $ids in order."""
        wb = write_batch()
        for i, props in enumerate(props_list):
            clean = {k: v for k, v in props.items() if v is not None}
            wb = wb.var_as(f"n{i}", g().add_n(label, clean).id())
        wb = wb.returning([f"n{i}" for i in range(len(props_list))])
        resp = self._send(wb)
        ids: list[int] = []
        for i in range(len(props_list)):
            val = resp[f"n{i}"]
            ids.append(int(val[0] if isinstance(val, list) else val))
        return ids

    def add_edges(self, edges: list[tuple[int, str, int]]) -> None:
        """Insert (from_id, label, to_id) edges in one write batch."""
        wb = write_batch()
        for i, (frm, label, to) in enumerate(edges):
            wb = wb.var_as(f"e{i}", g().n(NodeId(frm)).add_e(label, to=NodeId(to)).id())
        wb = wb.returning([f"e{i}" for i in range(len(edges))])
        self._send(wb)

    def drop_label(self, label: str) -> None:
        wb = write_batch().var_as("d", g().n_with_label(label).drop()).returning(["d"])
        self._send(wb)

    # ---- reads ----------------------------------------------------------
    def find_by_prop(self, label: str, prop: str, value: str,
                     props: list[str] | None = None) -> list[dict]:
        rb = read_batch().var_as(
            "r", g().n_with_label(label)
                   .where(Predicate.eq(prop, value))
                   .value_map(props or TITLE_PROPS)).returning(["r"])
        return self._send(rb)["r"] or []

    def all_genres(self) -> list[dict]:
        rb = read_batch().var_as(
            "r", g().n_with_label(GENRE_LABEL)
                   .value_map(["$id", "gid", "name"])).returning(["r"])
        return self._send(rb)["r"] or []

    def titles_in_from(self, from_ids: list[int], edge_label: str) -> list[dict]:
        """Titles on the far end of incoming `edge_label` edges from given nodes."""
        if not from_ids:
            return []
        rb = read_batch()
        for i, hid in enumerate(from_ids):
            rb = rb.var_as(f"v{i}", g().n(NodeId(hid)).in_(edge_label)
                             .value_map(["$id", "gid"]))
        rb = rb.returning([f"v{i}" for i in range(len(from_ids))])
        resp = self._send(rb)
        out: list[dict] = []
        for i in range(len(from_ids)):
            out.extend(resp[f"v{i}"] or [])
        return out

    def people_of_titles(self, title_ids: list[int]) -> dict[int, list[dict]]:
        """Map title HelixDB id -> connected person nodes (any person edge)."""
        if not title_ids:
            return {}
        rb = read_batch()
        for i, hid in enumerate(title_ids):
            rb = rb.var_as(f"v{i}", g().n(NodeId(hid)).out().value_map(["$id", "gid"]))
        rb = rb.returning([f"v{i}" for i in range(len(title_ids))])
        resp = self._send(rb)
        return {title_ids[i]: (resp[f"v{i}"] or []) for i in range(len(title_ids))}

    def all_titles_light(self) -> list[dict]:
        rb = read_batch().var_as(
            "r", g().n_with_label(TITLE_LABEL)
                   .value_map(["$id", "gid", "popularity"])).returning(["r"])
        return self._send(rb)["r"] or []
