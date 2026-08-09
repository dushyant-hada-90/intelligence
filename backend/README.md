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
python app.py
```

Stop any old Gradio process on port 7860 first, then restart.

## API

`GET /health` → `{ "ok": true }`

`POST /reels`

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

Server logs each decision as `decision id=... action=... comment=...`.

Docs UI: http://127.0.0.1:7860/docs
