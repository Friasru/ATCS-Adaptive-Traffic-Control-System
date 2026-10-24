# Monitor service and the ATCS web backend (Phase 3)

`web_service.py` runs the existing `MonitoringEngine` (same detector, tracker,
counting, recording and SQLite storage as the desktop app) behind a small HTTP
service on **127.0.0.1**. The Flask backend proxies to it under `/api/monitor/*`.
The browser never talks to the service directly, and the service secret never
leaves the server.

```
browser ──JWT──> Flask (backend/, its own venv) ──Bearer secret──> web_service.py (monitor/, its own venv)
                 /api/monitor/*                    127.0.0.1:8765     one MonitoringEngine
```

The two processes never share a Python interpreter. Both projects have a
top-level package called `app`, so do not add `monitor/` to the backend's path
or import one project from the other. The backend client
(`backend/app/services/monitor_client.py`) uses only the standard library.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `ATCS_MONITOR_SERVICE_SECRET` | both | Shared secret, at least 32 bytes. The service refuses to start without it; Flask answers 503 on every monitor endpoint without it. |
| `ATCS_MONITOR_PORT` | both | TCP port on 127.0.0.1, 1024-65535. Default `8765`. Must be the same on both sides. |
| `ATCS_MONITOR_CAMERAS` | service | Camera indices the web may select, e.g. `0` or `0,1`. Default: none. |
| `ATCS_MONITOR_VIDEO_DIR` | service | Folder whose video files may be selected. Default `monitor/data/test_media`. Only files directly inside it, with a video extension and a plain name (letters, digits, space, `_ . ( ) -`), are listed; symlinks that lead outside the folder are ignored. |
| `ATCS_MONITOR_HOME` | service | Optional data root (database, settings, recordings, logs, models), as for the desktop app. |

### Generating the secret

    python3 -c 'import secrets; print(secrets.token_hex(32))'

Put it in both shells (or in a local `.env` that you source; `.env` files are
git-ignored). Use a value different from `JWT_SECRET_KEY`. Never commit it and
never put it in frontend code.

### Who may use the monitor

Access is **deny by default** and is granted per account, on the server, with
`backend/monitor_access.py`:

    cd backend
    venv/bin/python monitor_access.py grant alberto operator   # the account must already exist
    venv/bin/python monitor_access.py grant ian viewer
    venv/bin/python monitor_access.py list
    venv/bin/python monitor_access.py revoke ian

Grants are stored in the `monitor_grants` table of `backend/atcs.db` (created
automatically; existing tables are not changed). No HTTP endpoint can create one,
and registering an account never does.

* No JWT, a malformed or expired JWT: **401** (same responses as the rest of the API).
* Valid JWT without a valid grant: **403**.
* Viewer: `GET` endpoints only; control endpoints return 403.
* Operator: everything. Operators are viewers too.

Roles are never read from the request or from JWT claims. A grant is bound to
one account: its user id, its username and a fingerprint of its stored password
hash. Because bcrypt hashes are salted, an account that is registered again
under a granted name does not match, even if it also gets the same id (SQLite
reuses ids after the database is recreated or the newest row is deleted). So:

* Recreating `atcs.db` removes every grant; nobody has monitor access until an
  administrator runs `grant` again for the new accounts.
* A user row deleted by hand and registered again under the same name gets 403.
* `list` shows such stale grants as `INVALID`; `revoke` removes them.
* If a password-change feature is added later, changing the password will also
  invalidate that account's grant (fail closed) until it is granted again.

## Running

Linux / WSL2 (two terminals):

    # terminal 1: monitor service (monitor environment)
    cd monitor
    export ATCS_MONITOR_SERVICE_SECRET=...      # the generated value
    export ATCS_MONITOR_PORT=8765
    .venv/bin/python web_service.py            # --verbose for request logs

    # terminal 2: Flask (backend environment)
    cd backend
    export JWT_SECRET_KEY=...                   # as before
    export ATCS_MONITOR_SERVICE_SECRET=...      # the same value as terminal 1
    export ATCS_MONITOR_PORT=8765
    venv/bin/python run.py
    # once, for existing accounts: venv/bin/python monitor_access.py grant alberto operator

Native Windows (PowerShell):

    cd monitor
    $env:ATCS_MONITOR_SERVICE_SECRET = "..."
    $env:ATCS_MONITOR_CAMERAS = "0"
    .venv-win\Scripts\python.exe web_service.py

and start Flask the same way from `backend\` with the same secret and port.
On Windows the service claims the port exclusively (`SO_EXCLUSIVEADDRUSE`) so
another process cannot bind the same port.

Stop the service with Ctrl+C (or SIGTERM / Ctrl+Break). It stops accepting
requests, stops monitoring, finalizes an active recording, closes the camera and
the database, and exits with code 0.

Each service owns one engine, and only one service can run per data folder: it
holds an exclusive lock on `data/monitor_service.lock` for its whole lifetime (a
second one exits with "another monitor service is already using this data
folder"). The operating system releases the lock when the process ends, even
if it crashes or is killed. Do not run the desktop app on the same camera at the
same time. Both use the same database, so sessions started from the web also
appear in the desktop app's History.

### Sessions left behind by a crash

The `sessions` table has no owner column, so the service records the id of each
session it creates in its own table, `web_service_sessions`, before creating
it. On startup, after taking the lock, it marks its own sessions that are still
`running` as `interrupted` ("Monitor service ended unexpectedly") and flags
their unfinished recordings, the same way the desktop app recovers its own.
Because the lock guarantees that no other service is using the folder, such a
session cannot belong to a live service. Sessions started by the desktop app are
never in that table and are never modified by the service, even while the
desktop app is monitoring.

The reverse is upstream behaviour that is unchanged here: when the desktop app
starts, it marks *every* `running` session as interrupted, including one the
service is running at that moment. When the service later stops that session
normally, its final status and statistics are written as usual.

### Testing with saved videos (WSL2)

WSL2 has no `/dev/video*` device by default, so cameras cannot be used there.
Download the test videos with `./scripts/fetch_test_media.sh` (they go to
`data/test_media/`, which is git-ignored) and leave `ATCS_MONITOR_CAMERAS`
unset. Video files play in real time; `"loop": true` replays them until stopped.
Physical cameras have to be tested on native Windows (see `docs/windows.md`).

## Service API (internal, 127.0.0.1)

Every request needs `Authorization: Bearer <secret>`; otherwise 401 before
anything else is looked at. Bodies are JSON objects of at most 4 KB.

| Method | Path | Result |
|---|---|---|
| GET | /status | live status (below) |
| GET | /sources | `{"sources": [{"id", "kind", "label"}]}` |
| GET | /sessions | the 20 most recent sessions: ids, status, times, counts, number of recordings (no paths) |
| GET | /frame.jpg | latest annotated frame, `image/jpeg`, `X-Frame-Index` header; 404 `no_frame` if none |
| POST | /start | `{"source": "<id from /sources>", "loop": false}`: 202 |
| POST | /stop | waits up to 10 s for the pipeline: 200 |
| POST | /recording/start | 202; recording begins with the next processed frame |
| POST | /recording/stop | 202; the file is finalized and validated in the background |

Errors are `{"error": "<message>", "code": "<code>"}`:
400 `invalid_body`/`invalid_source`/`unknown_source`, 401 `unauthorized`,
404 `not_found`/`no_frame`, 405 `method_not_allowed`, 409 `already_running`/
`not_running`/`not_ready`/`already_recording`/`not_recording`, 413, 415,
500 `internal_error`, 503 `busy`/`model_unavailable`/`shutting_down`.

At most 16 requests are handled at once (503 `busy` beyond that) and a client
has 10 s to send its request.

### Status document

Mapped from `MonitoringEngine.latest_snapshot()` and the engine state:

| Field | Meaning |
|---|---|
| `running`, `state`, `message` | pipeline thread alive; `Idle`/`Initializing`/`Detecting`/`Recording`/`Stopping`/`Error`; last state message |
| `source` | `{"id", "kind", "label"}` of the source started last |
| `session_id` | current (or last finished) session; `null` until the first frame of a new start |
| `recording` | `{"active", "file"}`; `file` is the file name only |
| `visible`, `unique`, `crossings` | per class: `person`, `car`, `motorcycle`, `bicycle`, `bus`, `truck` |
| `line_totals` | `{line_id: {"a_to_b": counts, "b_to_a": counts}}` |
| `totals` | `visible_/unique_/crossing_vehicles` = sum of `vehicle_classes` (`car`, `motorcycle`, `bus`, `truck`); `..._pedestrians` = `person`. Bicycles are in neither group; read them from the per-class counts. |
| `performance` | `capture_fps`, `inference_fps`, `recording_fps`, `latency_ms` (inference time of the last frame), `frames_processed`, `frames_dropped`, `recording_frames_dropped`, `active_tracks` |
| `device`, `model` | inference device; `{"name", "available"}` of the configured weights |
| `last_error` | last pipeline error (camera, model, inference, recording), or `null` |
| `last_session` | summary of the last finished session |

Messages that leave the service have local paths reduced to the file name.

The JPEG of a frame is encoded once and shared by all viewers; requesting
frames never triggers inference.

## Flask API

All need a JWT (`Authorization: Bearer <access_token>` from `/api/auth/login`).

| Method | Path | Role | Success |
|---|---|---|---|
| GET | /api/monitor/status | viewer | 200 status document |
| GET | /api/monitor/sources | viewer | 200 |
| GET | /api/monitor/sessions | viewer | 200 |
| GET | /api/monitor/frame.jpg | viewer | 200 `image/jpeg`, `Cache-Control: no-store` |
| POST | /api/monitor/start | operator | 202, body `{"source": "camera:0" or "video:<file>", "loop": bool}` |
| POST | /api/monitor/stop | operator | 200 |
| POST | /api/monitor/recording/start | operator | 202 |
| POST | /api/monitor/recording/stop | operator | 202 |

Order of checks: JWT (401), role (403), request body (400), configuration
(503), then the service call. Responses from the service:

| Situation | Flask answers |
|---|---|
| service 400 / 404 / 409, or 503 `model_unavailable`/`busy`/`shutting_down` | same status, `{"error", "code"}` |
| secret or port not configured | 503 `service_not_configured` |
| nothing listening on the port | 503 `service_unavailable` |
| no answer within 5 s (20 s for stop) | 504 `service_timeout` |
| service rejected the secret | 502 `service_auth_failed` (not 401, so the dashboard does not sign the user out) |
| any other service error or invalid response | 502 `service_error` / `bad_service_response` |

There are no file, recording or download endpoints, and no endpoint takes a
path. A wrong method on any `/api` route answers 405 with an `Allow` header,
including GET on POST-only routes (which used to fall through to the
static-file route and answer 404). Static files and other 404s are unchanged.

## Common problems

| Symptom | Fix |
|---|---|
| Service prints `ATCS_MONITOR_SERVICE_SECRET must be set` | Export a 32+ byte secret in that shell. |
| `cannot listen on 127.0.0.1:8765` | Another service (or program) uses the port. Stop it or pick another `ATCS_MONITOR_PORT` on both sides. |
| Flask 503 `service_not_configured` | Flask's shell lacks the secret, or the port is not 1024-65535. |
| Flask 503 `service_unavailable` | The service is not running, or the ports differ. |
| Flask 502 `service_auth_failed` | The two secrets differ. |
| Flask 403 for a user who should have access | `venv/bin/python monitor_access.py list`; grant the account (again) if it is missing or marked `INVALID`. No restart needed. |
| `another monitor service is already using this data folder` | Another service uses the same `ATCS_MONITOR_HOME`/monitor folder. Stop it, or give each its own data folder. |
| 503 `model_unavailable` | Run `scripts/download_model.py` or copy the weights into `models/`. |
| `/sources` has no videos | Put the files directly in `ATCS_MONITOR_VIDEO_DIR` (or `data/test_media/`) with a plain file name. |
| `last_error`: `Camera #0 does not exist (no /dev/video0)` | WSL2 has no camera; use a video, or run on Windows. |
| `409 not_ready` on recording start | Wait until `state` is `Detecting` (the model loads on the first start). |

## Tests

    cd monitor && .venv/bin/python -m pytest tests/test_web_service.py
    cd backend && venv/bin/pytest tests/test_monitor_api.py

Neither needs a camera, CUDA or the YOLO weights: the monitor tests use a
scripted detector and generated videos, and the backend tests replace the
service with a standard-library stub.

## Not done yet

* Phase 4: the browser page (`frontend/monitor.html`) using these endpoints.
* Phase 5: full session history, exports and recording downloads.
* Real webcams through the service have not been tested (the WSL2 development
  machine has no camera device).
