# Instagram Reel Timing + Server-Driven Autopilot

Chrome MV3 extension (MAIN world) that:

1. Intercepts Instagram Reels GraphQL / embedded Relay metadata
2. POSTs each **new reel batch** to the local FastAPI decision server (`POST /reels`)
3. Watches each reel for the server’s `duration` seconds
4. Performs `action` (`like` | `save` | none) and optional `comment` independently
5. Scrolls to the next reel

## Setup

1. Start the backend (see [`../backend/README.md`](../backend/README.md)) — `http://127.0.0.1:7860`
2. Chrome → `chrome://extensions` → Developer mode → **Load unpacked** → this folder (use **Reload** after updates)
3. Open `https://www.instagram.com/reels/`

API calls go through the extension **background service worker** (not the Instagram page), so Chrome does not block HTTPS→localhost. Server URL is `API_BASE_URL` in `content-script.js` (default `http://127.0.0.1:7860`).

## Console

By default only yellow `[REPORT] REEL_RESULT` lines appear — one per reel after watch+engage:

`PLAN action/comment/duration` vs `DONE dwell/action/comment` → `OK` or `FAIL`.

**`duration` = watch time only** (strict). Then like/save/comment run after, like a real user — engage time is logged but does not have to fit inside `duration`. If the plan includes a comment, watch is at least **15s**.

Set `QUIET_BOT_LOGS = false` / `QUIET_META_VIEWPORT_LOGS = false` in `content-script.js` for verbose diagnostics.

## Toggle

`AUTOPILOT_ENABLED` at the top of `content-script.js`.
