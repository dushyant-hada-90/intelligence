# Reel Intelligence API

FastAPI service with two pipelines:

1. **Hooks** — download an Instagram reel, Whisper transcript, shot cuts, **Amazon Nova 2 Lite** hook verdict + `cost_usd`.
2. **Discover** — scrape a business landing page, LLM keyword queries, rate-limited Instagram web GraphQL keyword search, top-N reels by engagement + recency.

## Setup

```bash
cd backend
pip install -r requirements.txt
# Copy .env.example → .env and fill keys
uvicorn app:app --host 127.0.0.1 --port 7860
```

Required env: `OPENAI_API_KEY` (Whisper + discover LLM), AWS credentials (Nova hooks), `INSTAGRAM_SESSIONID` (hooks download + discover search).

CLI (optional): `python hook_pipeline.py <reel_url>`

## Endpoints

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/health` | Liveness |
| `POST` | `/v1/hooks/analyze` | Body `{ "url": "<instagram reel>" }` → `{ "job_id", "status": "queued" }` |
| `GET` | `/v1/hooks/jobs/{job_id}` | Job status + result / error / cost |
| `GET` | `/v1/hooks/jobs/{job_id}/video` | Downloaded reel MP4 (use `?api_key=` if `API_KEY` set) |
| `POST` | `/v1/discover/analyze` | Body `{ "url": "<website>" }` → `{ "job_id", "status": "queued" }` |
| `GET` | `/v1/discover/jobs/{job_id}` | Tags, queries, ranked reels, warnings, LLM cost |
| `GET` | `/dev/` | **Developer UI only** (when `DEV_UI=1`) — hooks + discover tabs |

If `API_KEY` is set, send header `X-API-Key` on analyze/job routes.

## Discover notes

- Instagram calls use the browser GraphQL query `PolarisKeywordSearchExplorePageRelayQuery` (see `tunables.IG_KEYWORD_DOC_ID`). **Serialized** (1 at a time) with jittered delays (`DISCOVER_IG_MIN_DELAY_SEC` / `DISCOVER_IG_MAX_DELAY_SEC`) to reduce ban risk.
- Scoring uses `view_count` when present, else `like_count`, plus recency (`TREND_VIEWS_WEIGHT`, `TREND_RECENCY_HALF_LIFE_DAYS`). Tunables live in `tunables.py` / env overrides.
- Partial IG failures are soft: job still completes with `warnings`.

## Concurrency

Hook jobs: `MAX_CONCURRENT_JOBS` / `MAX_QUEUE_SIZE`.  
Discover jobs: `DISCOVER_MAX_CONCURRENT_JOBS` / `DISCOVER_MAX_QUEUE_SIZE`.  
Beyond queue limits, `POST` returns **503**.

## Production notes

- Set `DEV_UI=0` so `/dev/` is disabled.
- Prefer a single uvicorn worker with this in-memory job store (or move to Redis/Celery for multi-process).
- `/dev/` is for local testing only — not a production frontend.
- Keep `INSTAGRAM_SESSIONID` fresh; expired sessions break download and keyword search.
