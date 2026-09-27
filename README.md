# pickfive-helix — Pick Five graph service (Slice 2)

HelixDB (Docker, Fly.io, region dfw) loaded with the Pick Five catalog graph,
fronted by a small FastAPI service. Pick Five calls this over HTTP.

- App: `pickfive-helix` → https://pickfive-helix.fly.dev
- Fly region: dfw. Data persists on the `helix_data` volume (`/data`,
  `HELIX_DATA_DIR`), so restarts are fast; the loader is idempotent and
  re-runs safely any time.
- Catalog: 8,800 titles / 12,994 nodes / 28,349 edges (see manifest.json).

## Graph model

Labels: `Title`, `Person`, `Genre`. Edges (title → person/genre):
`directed_by`, `created_by`, `authored_by`, `has_genre`.

Title nodes carry the catalog id in `gid` (e.g. `movie:tmdb:969681`) plus
`title, year, type, description, image_url, popularity, vote_average,
vote_count`. Person nodes: `gid, name, role`. Genre nodes: `gid, name`.
All values are verbatim from `catalog/graph.jsonl`; nothing is invented.

## API contract (frozen for Slice 3)

### GET /health
Response 200:
```json
{"status": "ok", "title_count": 8800}
```
While the catalog is loading in the background (first boot), 200 with
`{"status": "loading", "title_count": <live count>}`; the query endpoints
return 503 until the load verifies. 503
`{"status": "degraded", "title_count": 0}` when HelixDB is unreachable,
503 `{"status": "failed", ...}` when the load itself failed (Fly restarts
the machine and the loader retries).

### POST /contenders
Request body — either shape is accepted:
```json
{"taste_vector": {"genres": {"Science Fiction": 1.0},
                  "people": {"person:tmdb:1144604": 0.8},
                  "titles": {"movie:tmdb:969681": 1.0},
                  "exclude": ["movie:tmdb:111]},
 "limit": 20}
```
or the taste_vector object itself as the body.

`taste_vector` fields (all optional):
- `genres`: genre **name** → weight (e.g. `"Science Fiction"`)
- `people`: person **gid** → weight (e.g. `"person:tmdb:1144604"`)
- `titles`: liked title **gid** → weight; also triggers "more like this"
  (titles sharing people with the liked title, +0.5 × weight each)
- `exclude`: title gids to skip (also skips the liked `titles` themselves)
- `limit`: max ids to return

Response 200 — **bare JSON array** of title gids, best first:
```json
["movie:tmdb:27205", "movie:tmdb:157336", "..."]
```

Scoring v1: sum of matched genre weights + matched person weights +
liked-title person-overlap bonus. Empty taste vector → most popular titles.

### POST /next-five
Same body shapes as /contenders. Response: bare JSON array of **5** title
gids (or fewer when the graph cannot fill 5). Default limit 5; an explicit
`limit` overrides it.

### GET /search?name=<exact title or person name>&kind=title|person
Helper for spot checks and Slice 3 lookups. Returns matching nodes:
```json
[{"$id": 7, "gid": "movie:tmdb:27205", "title": "Inception", "year": 2010, ...}]
```
`kind=person` matches on the `name` property instead.

## Operations

- Deploy: GitHub Actions (`.github/workflows/deploy.yml`) runs
  `flyctl deploy --remote-only` on push to main or manual dispatch. This
  VM cannot reach Fly's remote builders through the egress proxy, so
  `fly deploy` is never run here. Needs the `FLY_API_TOKEN` repo secret
  (a Fly deploy token for the `pickfive-helix` app).
- Volume (created once): `fly volumes create helix_data --region dfw --size 1`
- Reload catalog manually: `fly ssh console -C "python3 /srv/loader.py
  --helix-url http://127.0.0.1:8080 --graph /srv/graph.jsonl
  --manifest /srv/manifest.json"`
- Startup: supervisor starts helix-server, then uvicorn; the API loads the
  catalog in a background thread so health checks answer in seconds.
  `/health` reports `loading` until node/edge counts verify against
  manifest.json, then flips to `ok`. Restarts on a warm volume skip the
  load instantly (counts already match).
- Load log: `~/workspace/pick-five/hidden_files/helix-load-<date>.md`
