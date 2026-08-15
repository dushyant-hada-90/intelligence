# Reel Hook Analyzer API (Nova main stack)

FastAPI service that downloads an Instagram reel, transcribes the opening with Whisper, detects shot cuts, and runs **Amazon Nova 2 Lite** for the hook verdict. Returns estimated **cost_usd** on completed jobs.

## Setup

```bash
cd backend
pip install -r requirements.txt
# Copy .env.example → .env and fill keys
uvicorn app:app --host 127.0.0.1 --port 7860
```

Required env: `OPENAI_API_KEY` (Whisper), AWS credentials (Nova), `INSTAGRAM_SESSIONID` (or browser cookies / `cookies.txt` in this folder).

CLI (optional): `python hook_test.py <reel_url>`

## Endpoints

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/health` | Liveness |
| `POST` | `/v1/hooks/analyze` | Body `{ "url": "..." }` → `{ "job_id", "status": "queued" }` |
| `GET` | `/v1/hooks/jobs/{job_id}/video` | Downloaded reel MP4 (use `?api_key=` if `API_KEY` set) |
| `GET` | `/dev/` | **Developer UI only** (when `DEV_UI=1`) — video player + cut marks |

If `API_KEY` is set, send header `X-API-Key` on analyze/job routes.

## Concurrency

Jobs run in a bounded `ThreadPoolExecutor` (`MAX_CONCURRENT_JOBS`, default 3). Extra work queues up to `MAX_QUEUE_SIZE`; beyond that, `POST` returns **503**.

## Production notes

- Set `DEV_UI=0` so `/dev/` is disabled.
- Prefer a single uvicorn worker with this in-memory job store (or move to Redis/Celery for multi-process).
- `/dev/` is for local testing only — not a production frontend.
