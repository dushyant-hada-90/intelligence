# Reel Intelligence API

FastAPI service with two pipelines:

1. **Hooks** — download an Instagram or TikTok video, Whisper transcript, shot cuts, **Amazon Nova 2 Lite** hook verdict, plus LLM `whoWatched` / `whyWatched` bullet strings and `cost_usd`.
2. **Discover** — scrape a business landing page, LLM keyword queries, multi-platform search (`platforms[]`), top-N reels by engagement + recency.

## Setup

```bash
cd backend
pip install -r requirements.txt
# Copy .env.example → .env and fill keys
uvicorn app:app --host 127.0.0.1 --port 7860
```

Required env: `OPENAI_API_KEY` (Whisper + discover LLM), AWS credentials (Nova hooks).  
Instagram discover/download: `INSTAGRAM_SESSIONID`.  
TikTok download uses yt-dlp guest. TikTok **discover search** uses Playwright Chromium on a dedicated thread.
One-time setup (in the same venv you run uvicorn from):

```bash
pip install playwright
playwright install chromium
```

If launch still says executable missing, clear any custom `PLAYWRIGHT_BROWSERS_PATH` and re-run `playwright install chromium`.

CLI (optional): `python hook_pipeline.py <instagram_or_tiktok_url>`

## Endpoints

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/health` | Liveness + discover platform list |
| `POST` | `/v1/hooks/analyze` | Body `{ "url": "<instagram or tiktok>" }` → `{ "job_id", "status": "queued" }` |
| `GET` | `/v1/hooks/jobs/{job_id}` | Job status + result (`nova_hook`, `whoWatched`, `whyWatched`) / error / cost |
| `GET` | `/v1/hooks/jobs/{job_id}/video` | Downloaded MP4 (use `?api_key=` if `API_KEY` set) |
| `GET` | `/v1/discover/platforms` | `{ "platforms": [{ "name", "label" }, ...] }` |
| `POST` | `/v1/discover/analyze` | Body `{ "url": "<website>", "platforms": ["instagram","tiktok"] }` — omit `platforms` → `["instagram"]` |
| `GET` | `/v1/discover/jobs/{job_id}` | Tags, queries, ranked reels (each with `platform`), warnings, LLM cost |
| `GET` | `/dev/` | **Developer UI only** (when `DEV_UI=1`) — hooks + discover tabs |

If `API_KEY` is set, send header `X-API-Key` on analyze/job routes.

## Platforms (modular)

Search adapters register in `platforms.py`. Current: `instagram`, `tiktok`.  
Adding another network: implement `*_search.py` (+ hook URL/download if needed) → `register(PlatformSpec(...))`.

Each discover reel includes `platform` for UI badges. Dedupe key is `(platform, id)`.

## Discover notes

- Instagram: browser GraphQL `PolarisKeywordSearchExplorePageRelayQuery` (see `tunables.IG_KEYWORD_DOC_ID`). Serialized with jitter (`DISCOVER_IG_*_DELAY_SEC`).
- TikTok: discover search via **Playwright** (dedicated thread). Defaults to **headful Google Chrome** (`TIKTOK_PLAYWRIGHT_HEADLESS=0`, `TIKTOK_PLAYWRIGHT_CHANNEL=chrome`) because headless Chromium often gets empty `item/full` bodies. Soft-fails into `warnings`. Analyze/download still uses yt-dlp.
- Scoring uses `view_count` / `playCount` when present, else likes, plus recency (`TREND_VIEWS_WEIGHT`, `TREND_RECENCY_HALF_LIFE_DAYS`).
- Partial platform failures are soft: job still completes with `warnings`.

## Concurrency

Hook jobs: `MAX_CONCURRENT_JOBS` / `MAX_QUEUE_SIZE`.  
Discover jobs: `DISCOVER_MAX_CONCURRENT_JOBS` / `DISCOVER_MAX_QUEUE_SIZE`.  
Beyond queue limits, `POST` returns **503**.

## Production notes

- Set `DEV_UI=0` so `/dev/` is disabled.
- Prefer a single uvicorn worker with this in-memory job store (or move to Redis/Celery for multi-process).
- `/dev/` is for local testing only — not a production frontend.
- Keep `INSTAGRAM_SESSIONID` fresh; expired sessions break IG download and keyword search.
