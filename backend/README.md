# Reel Intelligence API

FastAPI service with two pipelines:

1. **Hooks** — download an Instagram or TikTok video, Whisper transcript, shot cuts, **Amazon Nova 2 Lite** (hook verdict + `whoWatched` / `whyWatched`), and `cost_usd`.
2. **Discover** — scrape a business landing page (Firecrawl when configured), LLM keyword queries, multi-platform search (`platforms[]`), top-N reels by engagement + recency.

## Layout

```
backend/
  app.py              # uvicorn entry (re-exports api.app)
  api/                # FastAPI routes
  config/             # env + tunables
  shared/             # http, llm, costs, job base
  platforms/          # registry + instagram/tiktok search
  discover/           # scrape, query gen, pipeline, jobs
  hooks/              # download, nova, pipeline, jobs
  static/             # /dev UI
```

## Setup

```bash
cd backend
pip install -r requirements.txt
# Copy .env.example → .env and fill keys
uvicorn app:app --host 127.0.0.1 --port 7860
```

Required env: `OPENAI_API_KEY` (Whisper + discover LLM), AWS credentials (Nova hooks).  
Discover landing scrape: set `FIRECRAWL_API_KEY` for Firecrawl (JS render + clean markdown); without it, falls back to httpx + BeautifulSoup. Override with `DISCOVER_SCRAPE_BACKEND=auto|firecrawl|httpx`.  
Instagram discover/download: `INSTAGRAM_SESSIONID`.  
TikTok discover search: logged-in cookies in `.env` — `TIKTOK_SESSIONID` (`sid_tt`), plus `TIKTOK_MS_TOKEN` / `TIKTOK_TTWID` when possible (or `TIKTOK_COOKIES`).  
TikTok analyze/download: yt-dlp (guest).

CLI (optional): `python -m hooks.pipeline <instagram_or_tiktok_url>`

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

Search adapters register in `platforms/`. Current: `instagram`, `tiktok`.  
Adding another network: implement `platforms/<name>/search.py` → `register(PlatformSpec(...))`.

Each discover reel includes `platform` for UI badges. Dedupe key is `(platform, id)`.

## Discover notes

- Landing page: **Firecrawl** when `FIRECRAWL_API_KEY` is set (`DISCOVER_SCRAPE_BACKEND=auto`); on failure or missing key, **httpx + BeautifulSoup**. Force with `firecrawl` or `httpx`.
- Instagram: browser GraphQL `PolarisKeywordSearchExplorePageRelayQuery` (see `config.tunables.IG_KEYWORD_DOC_ID`). Serialized with jitter (`DISCOVER_IG_*_DELAY_SEC`).
- TikTok: discover search via **httpx + logged-in `.env` cookies** (`TIKTOK_SESSIONID` / `sid_tt`, ideally also `TIKTOK_MS_TOKEN` + `TIKTOK_TTWID`). Soft-fails into `warnings` with http status/body debug. Analyze/download still uses yt-dlp.
- Keep `TIKTOK_SESSIONID` fresh the same way as Instagram’s session cookie.
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
