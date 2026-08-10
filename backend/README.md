# Reel decision API (FastAPI)

Local JSON API for the Instagram reel timing extension. Binds to `http://127.0.0.1:7860`.

Gradio was removed — a plain FastAPI app is simpler and scales better for batch `POST` decisions (no UI runtime, predictable OpenAPI, easy to put behind a real process manager later).

## Setup

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Fill SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in .env
python app.py
```

One-time Supabase SQL (SQL editor): run [`sql/reels_updated_at.sql`](sql/reels_updated_at.sql) so `updated_at` bumps on upsert.

Stop any old process on port 7860 first, then restart.

## API

`GET /health` → `{ "ok": true }`

### `POST /reels` (decisions — unchanged)

Request:

```json
[{ "id": "SHORTCODE1" }, { "id": "SHORTCODE2" }]
```

Response:

```json
[
  { "id": "SHORTCODE1", "action": "like", "comment": "Nice one.", "duration": 6.42 },
  { "id": "SHORTCODE2", "action": null, "comment": null, "duration": 4.1 }
]
```

- `action`: `"like"` | `"save"` | `null`
- `comment`: string or `null` (**independent** of `action`; stub uses ~50% comment rate)
- `duration`: **watch seconds only** (engage happens after). If `comment` is set, watch duration is at least **15**.

### `POST /reels/ingest` (Supabase upsert)

Called by the extension **after the bot watches a reel** (not on GraphQL prefetch). Prefetched-but-unwatched reels are never written.

Request:

```json
[
  {
    "id": "SHORTCODE1",
    "username": "some_user",
    "music": "Original audio · @some_user",
    "likes": 12,
    "comments": 3,
    "reposts": 1
  }
]
```

`likes` may be `null` when Instagram hides like counts (`like_and_view_counts_disabled`). Run [`sql/reels_likes_nullable.sql`](sql/reels_likes_nullable.sql) once so the column accepts NULL.

Response: `{ "upserted": 1 }`

Upserts by `id` (shortcode). Refreshes `username`, `music`, `likes`, `comments`, `reposts`, `updated_at`. Leaves `breakthrough` / `score` / `deeper_insights` / `created_at` alone. Missing env or Supabase errors → HTTP 502.

Docs UI: http://127.0.0.1:7860/docs
