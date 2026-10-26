# shadowdiff

Record real API traffic, replay it against a new version before you ship, and flag only the
response changes that matter.

Unit and integration tests only cover the cases engineers think of, so regressions like a renamed
field, a changed type or a flipped status code slip through to production. shadowdiff replays the
requests your users actually sent and reports structural differences, ignoring noise like
timestamps, generated IDs and reordered lists.

> On PyPI the package is named **`traffic-diff`** (`shadowdiff` is too close to an existing
> project there). The command and the Python package are still `shadowdiff`.

> Status: MVP (v0.1.0). `GET` requests only. See [Limitations](#limitations).

## How it works

```
             record                         replay
 clients ──► shadowdiff proxy ──► API v1    shadowdiff replay ──► API v2
              (:8080)    │                        │
                         ▼                        ▼
                    shadowdiff.db  ─────►  smart diff ──► report.md, exit code
```

1. **Record.** `shadowdiff record` runs a reverse proxy in front of the current version. It forwards
   every request unchanged and records `GET` request/response pairs (2xx-4xx, bodies up to 1 MB) to
   SQLite. Recording happens off the response path: exchanges go onto an in-memory queue that a
   background task flushes in batches.
2. **Replay.** `shadowdiff replay` sends the recorded requests to the new version with async httpx,
   a concurrency limit and a 10 s timeout. Timeouts and connection errors are recorded as failures,
   not crashes.
3. **Smart diff.** Each JSON response is walked against the recording. Every difference gets a
   JSON path and a kind: `type_changed`, `field_removed` and `status_changed` are **breaking**;
   `field_added` and `value_changed` are warnings. Ignore rules in `shadowdiff.yaml` drop the noise.
4. **Report.** Identical diffs are grouped across requests ("`$.price` type changed number → string
   on 844 requests to `GET /products/{id}`") and written to `report.md`. The CLI exits **1** if
   there is any breaking diff or request error, so it works as a CI gate.

## Quickstart

```bash
git clone <this repo> && cd shadowdiff
pip install -e ".[dev]"             # once published: pip install traffic-diff
```

Run the demo shop: v1 on `:8001`, a buggy v2 on `:8002`, and the recorder on `:8080` in front of v1.

```bash
docker compose up -d --build
python demo/traffic.py --requests 2000      # ~2,000 realistic GETs through the recorder
docker compose stop recorder                # flush and close the recording database
shadowdiff replay --session demo --target http://localhost:8002 --db data/shadowdiff.db
echo $?                                     # 1: breaking changes found
shadowdiff report --db data/shadowdiff.db --format html   # re-render the latest run as HTML
docker compose down
```

Without Docker, run the same three processes locally:

```bash
VERSION=v1 uvicorn demo.app:app --port 8001 &
VERSION=v2 uvicorn demo.app:app --port 8002 &
shadowdiff record --target http://127.0.0.1:8001 --port 8080 --session demo &
python demo/traffic.py --base-url http://127.0.0.1:8080
kill %3                                     # stop the recorder
shadowdiff replay --session demo --target http://127.0.0.1:8002 --concurrency 50
```

### Commands

| Command | Does |
| --- | --- |
| `shadowdiff record --target URL [--port 8080] [--session NAME]` | Run the recording proxy |
| `shadowdiff replay --session NAME --target URL [--concurrency 20] [--config shadowdiff.yaml]` | Replay, diff, write `report.md`, exit 1 on breaking diffs |
| `shadowdiff report [--run RUN_ID] [--format md\|html]` | Re-render a run's report (default: latest run) |

All commands take `--db` (default `shadowdiff.db`). Run any command with `--help` for every option.

### Ignore rules (`shadowdiff.yaml`)

```yaml
ignore_fields: ["*_at", "id", "request_id"]   # field-name globs; "$.meta.*" matches a full path
ignore_values: [uuid, iso_timestamp]          # a UUID -> other UUID change is noise
unordered_lists:
  "$.items": "id"                             # match list items by key, not position
```

Unordered-list paths can use `[*]` for nested lists (`"$.orders[*].lines": "sku"`), and a `null`
key compares the list as a multiset.

## Sample report

From the demo: v1 traffic replayed against v2, which returns `price` as a string and renames
`in_stock` to `available` on `/products/{id}`. Each response also carries a fresh `request_id`
UUID and `served_at` timestamp, and `/products` returns its items in random order. None of that
noise shows up.

> **FAIL**: 2 breaking change(s) across 844 request(s)
>
> | Replayed | Passed | Failed | Breaking changes | Warnings | Duration |
> | --- | --- | --- | --- | --- | --- |
> | 2001 | 1157 | 844 | 2 | 1 | 1.93s |
>
> **Breaking changes**
>
> | Endpoint | Path | Change | Requests | Example |
> | --- | --- | --- | --- | --- |
> | `GET /products/{id}` | `$.in_stock` | field removed | 844 | `/products/20`: was `false` |
> | `GET /products/{id}` | `$.price` | type changed number → string | 844 | `/products/20`: `128.85` → `"128.85"` |
>
> **Warnings**
>
> | Endpoint | Path | Change | Requests | Example |
> | --- | --- | --- | --- | --- |
> | `GET /products/{id}` | `$.available` | field added | 844 | `/products/20`: added `false` |
>
> **By endpoint**
>
> | Endpoint | Requests | Passed | Failed |
> | --- | --- | --- | --- |
> | `GET /health` | 93 | 93 | 0 |
> | `GET /orders/{id}` | 388 | 388 | 0 |
> | `GET /products` | 605 | 605 | 0 |
> | `GET /products/{id}` | 914 | 70 | 844 |

Replaying the same recording against **v1** reports 0 breaking changes and 0 warnings, and exits 0.
That run is the check that noise filtering works.

## Measured numbers

Measured on an Apple M3 laptop (8 cores) with everything on one machine, using
`demo/traffic.py` (2,000 requests) and `demo/bench.py` (6,000 requests per side, direct and proxied
rounds alternated). Other desktop apps were running, so treat these as indicative.

| Metric | Result | Target |
| --- | --- | --- |
| Planted bugs caught | **2 / 2** | 2 / 2 |
| False positives from noise (v1 vs v1) | **0** | 0 |
| Replay throughput, 2,000 requests at concurrency 50 | **~1,050-1,300 req/s** | ≥ 500 req/s |
| Proxy overhead, sequential requests | **+1.0 ms p50, +1.2 ms p95** | < 5 ms p95 |
| Proxy overhead, 10 concurrent clients | +3.4-4.6 ms p50, +5.7-13.8 ms p95 | |

The single-process demo API tops out around 1,100 req/s, so replay throughput is bounded by the
target here, not by shadowdiff. At 10 concurrent clients the single-process proxy is close to
saturation on one core, which is where the higher, noisier p95 comes from.

Reproduce:

```bash
python -m demo.bench --direct http://127.0.0.1:8001 --proxy http://127.0.0.1:8080 --concurrency 1
shadowdiff replay --session demo --target http://127.0.0.1:8001 --concurrency 50
```

## Limitations

- Only `GET` requests are recorded and replayed. Safe replay of writes is the next big piece.
- Recorded headers and bodies are stored as-is in SQLite, including `Authorization` headers and
  cookies. Don't record production traffic with credentials or PII until redaction lands.
- `value_changed` is a warning, not breaking (per the PRD), so a changed value alone won't fail CI.
- The proxy is a single process. Under heavy concurrent load its added latency grows.
- Bodies are compared fully in memory, and diffs for a run are computed after all requests return.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && pytest
```

## License

MIT
