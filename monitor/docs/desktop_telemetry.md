# Desktop telemetry: ATCS Monitor desktop → ATCS backend (Phase 5a)

The desktop ATCS Monitor app (PySide6, normally on native Windows) is the
camera-monitoring interface. The ATCS web backend stores what it reports: live
status, counts and finished sessions. Data flows **only from the desktop to
Flask**. Flask never sends commands to a desktop, and no video leaves it.

```
desktop ATCS Monitor ──device token──> Flask /api/desktop/telemetry/*  (write only)
browser              ──JWT + grant──> Flask /api/monitor/desktop/*    (read only)
```

Phase 5a is the backend only. The desktop side that sends these documents is
the Phase 5b desktop link ([desktop_link.md](desktop_link.md)); the Monitor page
that shows them (Phase 5c) comes later. The Phase 3/4 monitor service and
`/api/monitor/*` work as before.

## 1. Registering a desktop (device setup)

Devices are created **only** on the server, with the local admin script. No
HTTP endpoint can create a device or issue a credential.

    cd backend
    venv/bin/python monitor_access.py device add alberto-laptop   # prints the token once
    venv/bin/python monitor_access.py device list
    venv/bin/python monitor_access.py device revoke alberto-laptop

* Names: 3 to 64 letters, digits, `.`, `_` or `-`. A name can't be reused,
  even after it is revoked, so stored sessions keep an unambiguous origin.
* `device add` prints the token **once**. Only its SHA-256 is stored in
  `backend/atcs.db` (`monitor_devices.token_hash`). If the token is lost,
  revoke the device and add a new one.
* `device revoke` stops the token at once (the next request gets 401). The
  device's stored sessions are kept, and its live status disappears from
  `/api/monitor/desktop/status`.
* `device list` shows names, state and last-seen times. It never shows tokens
  or hashes.

### Credential handling

* Token format: `atcsdev_` followed by 43 URL-safe characters (32 random bytes
  from `secrets`).
* Treat it like a password. Do not commit it, paste it into a browser or put it
  in frontend code. Phase 5b keeps it in the desktop's local link
  configuration in the user's own data folder.
* It is sent as `Authorization: Bearer atcsdev_...`, to loopback only (section 4).
* The backend never logs tokens or their hashes. Failed credentials are logged
  without the value, at most 10 warnings per minute.
* Device tokens and browser JWTs are separate:
  * a device token is not a JWT, so every browser endpoint answers 401 to it;
  * the telemetry endpoints accept only device tokens, so a JWT gets 401
    there, even an operator's.

## 2. Endpoints

### Device endpoints (write only)

All are `POST` with `Content-Type: application/json` and a device token.

| Path | Body | Limit | Success |
|---|---|---|---|
| `/api/desktop/telemetry/status` | `desktop_status` | 8 KB | 200 `{"accepted": true, "server_time", "heartbeat_timeout_s"[, "session": "live"\|"finalized"]}`, or 200 `{"accepted": false, "reason": "stale"\|"superseded"}` |
| `/api/desktop/telemetry/sessions/<session_id>/events` | `crossing_events` | 128 KB | 200 `{"inserted", "duplicates", "stored_events"}` |
| `/api/desktop/telemetry/sessions/<session_id>/summary` | `session_summary` | 32 KB | 201 `{"finalized": true, "duplicate": false, "status", "counts_origin"}`; the same summary again: 200 `{"duplicate": true}` |

Order of checks:

1. network origin (403 `origin_refused` / `browser_refused`)
2. device token (401 `device_unauthorized`)
3. per-device rate limit (429 `rate_limited`, `Retry-After`)
4. content type (415)
5. length (411 / 413)
6. JSON (400 `invalid_json`)
7. schema (400 `invalid_document`)
8. state checks:

| Code | Meaning |
|---|---|
| 409 `session_conflict` | the session id belongs to another device |
| 409 `session_mismatch` | start time or source differ from the stored session |
| 409 `session_instance_mismatch` | another desktop process reported this session live |
| 409 `event_conflict` | an event key is already stored with other values |
| 409 `session_finalized` | new events for a finalized session |
| 409 `events_incomplete` | the stored events don't match the summary; the response includes `stored_events` |
| 409 `summary_conflict` | a different summary was already stored |
| 422 `counter_decreased` | a cumulative counter went down |
| 422 `clock_skew` | a desktop timestamp is more than 5 min in the future, or `sent_at` more than 5 min in the past |
| 422 `invalid_timestamp` | an event is before its session's start |

Errors are `{"error": "<message>", "code": "<code>"}`. A refused request changes nothing.

### Browser endpoints (read only)

These need a JWT and a viewer or operator grant, like the rest of `/api/monitor/*`:
401 for a missing or expired token, 403 without a grant.

| Path | Result |
|---|---|
| `GET /api/monitor/desktop/status` | `{"heartbeat_timeout_s", "server_time", "devices": [...]}`, one entry per active device |
| `GET /api/monitor/desktop/sessions?limit=1..200&device=<name>` | `{"sessions": [...]}`, newest first (default 50) |
| `GET /api/monitor/desktop/sessions/<session_id>` | one session with `line_totals`, `recordings`, `model`, `crossing_event_count`, `provenance` |

**Device entries** use the field names of the Phase 4 status document
(`running`, `state`, `message`, `source`, `session_id`, `recording`, `visible`,
`unique`, `crossings`, `line_totals`, `totals`, `performance`, `device`,
`last_error`), so the Phase 5c page can reuse the existing renderers. They also have:

* `desktop`: `name`, `connection` (`never_connected` / `connected` /
  `disconnected`), `last_seen_at` (server clock), `seconds_since_seen`, `app`
  and `desktop_sent_at` (desktop clock);
* `live`: true while the last heartbeat is less than `heartbeat_timeout_s`
  (15 s) old.

When a device is not live, `running` and `recording.active` are false. The
other values are the last ones it reported.

**Sessions** have `status` (`running` / `completed` / `interrupted` / `error`),
`finalized`, `live`, `counts_origin`, the desktop's `started_at` / `ended_at`,
and the server's `first_received_at` / `last_received_at` / `finalized_at`.
A `running` session with `live: false` belongs to a desktop process that has
stopped reporting. Its summary normally arrives when that desktop starts again.

## 3. Documents (schema 1.0)

Class keys, sources and directions follow `integration_contract.md`.
Unknown fields, missing fields, wrong types, `NaN`/`Infinity` and duplicate JSON
keys are rejected. Timestamps are ISO 8601 with an offset.

### `desktop_status` (heartbeat; once per second while running, every 10 s idle)

```json
{
  "schema_version": "1.0", "kind": "desktop_status",
  "instance_id": "<32 hex, new for each desktop process>",
  "instance_started_at": "2026-10-23T12:00:00.000+00:00",
  "seq": 1234,
  "sent_at": "2026-10-23T12:20:34.000+00:00",
  "app": {"name": "ATCS Monitor", "version": "1.0.0"},
  "state": "Detecting",
  "message": "Detecting on camera:0 (cuda:0, 960 px)",
  "camera_status": "ok",
  "source": {"kind": "camera", "label": "Integrated Camera"},
  "recording": {"active": false, "file": null},
  "session": {
    "session_id": "<32 hex>", "started_at": "...", "timestamp": "...",
    "visible":   {"person": 1, "car": 2, "motorcycle": 0, "bicycle": 0, "bus": 0, "truck": 0},
    "unique":    {...six classes...},
    "crossings": {...six classes...},
    "line_totals": {"line-1": {"a_to_b": {...}, "b_to_a": {...}}},
    "frames_processed": 812
  },
  "performance": {"capture_fps": 29.8, "inference_fps": 10.4, "recording_fps": 0.0, "latency_ms": 11.2,
                  "frames_dropped": 0, "recording_frames_dropped": 0, "active_tracks": 3},
  "device": "cuda:0",
  "last_error": null
}
```

* `state`: `Idle`, `Initializing`, `Detecting`, `Recording`, `Stopping`, `Error`.
* `camera_status`: `ok`, `reconnecting`, `error`, `none`.
* `session` is `null` when nothing is being monitored; `source` is then optional.
* `device` is `cpu`, `cuda:0` and so on, or `null`.

### `crossing_events` (pages of 1 to 500)

```json
{"schema_version": "1.0", "kind": "crossing_events", "session_id": "<32 hex>",
 "started_at": "...", "source": "camera", "source_id": "Integrated Camera",
 "events": [{"timestamp": "...", "line_id": "line-1", "track_id": 17, "class": "car", "direction": "a_to_b"}]}
```

The key `(session_id, line_id, track_id, direction)` is unique, as in the
contract. A page may not repeat a key.

### `session_summary`

This is the contract's `session_summary` (the History → Export JSON document)
with two changes:

* `crossing_events` is **removed**; events are uploaded as pages first.
* Two fields are added:
  * `crossing_event_count`: number of crossing events in the session; it must
    equal the sum of `crossings`.
  * `stats_complete`: `false` only for an `interrupted` or `error` session
    whose statistics were never saved, because the desktop app crashed.

`status` must be `completed`, `interrupted` or `error`. `ended_at` is required.

### Names and paths

These must be **bare names**: camera labels, `source_id`, recording `file`,
`model.name` and `line_id`. Anything containing `/ \ : < > | " ? *`, `..` or
control characters is **rejected**, not trimmed. No client-supplied path is ever
stored or used as a path. Free text (`message`, `last_error`) is display-only:
control characters are removed, and any path in it is reduced to its last
component (`C:\Users\x\clip.mp4` becomes `clip.mp4`), then cut to 300 characters.

## 4. Network origins and Windows-to-WSL2 connectivity

**Accepted:** requests whose TCP peer is a loopback address: `127.0.0.0/8`,
`::1`, and `::ffff:127.0.0.1`. That's all.

**Refused:** everything else, with 403 `origin_refused`. Also refused:

* requests carrying `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host` or
  `X-Real-IP`, because a local reverse proxy would make remote clients look local;
* requests carrying `Origin`, `Cookie`, `Sec-Fetch-Site` or `Sec-Fetch-Mode`
  (403 `browser_refused`). Browsers send these; the desktop client never does.

No CORS headers are ever sent, so a web page can't read the responses either.

There is no setting to accept remote devices. A desktop on another computer
would need HTTPS and a deliberate change to this rule. That's not part of Phase 5a.

**Checked on the development machine (2026-10-24).** Windows 11 with WSL2 in
the default NAT networking mode (no `networkingMode` in `.wslconfig`):

| Client → server | Peer address Flask saw | Result |
|---|---|---|
| Windows PowerShell → `http://127.0.0.1:<port>` (Flask in WSL bound to 127.0.0.1) | `127.0.0.1` | 200, accepted |
| Windows PowerShell → `http://localhost:<port>` | `127.0.0.1` | 200, accepted |
| WSL `curl` → `http://127.0.0.1:<port>` | `127.0.0.1` | 200, accepted |
| Windows PowerShell → `http://<WSL eth0 IP>:<port>` (Flask bound to 0.0.0.0) | `172.17.0.1` (the Windows host's virtual adapter) | 403, refused |

So with the default `venv/bin/python run.py` (127.0.0.1:5000), a desktop on the
same Windows computer reaches Flask through WSL's localhost forwarding as
`http://127.0.0.1:5000`, and is seen as loopback.

**Not verified, and needing their own check before use:**

* WSL **mirrored** networking mode;
* `localhostForwarding=false` in `.wslconfig`. Windows then cannot reach
  `127.0.0.1` in WSL at all, and the desktop reports the backend as unreachable;
* Flask behind a reverse proxy (refused by design, see above);
* Flask running natively on Windows. That should be plain loopback, but it has not been tried.

To repeat the check after changing WSL settings: start Flask, register a test
device, and `POST` a heartbeat from Windows PowerShell with
`Invoke-WebRequest -Method Post -ContentType application/json -Headers @{Authorization='Bearer <token>'} -InFile status.json http://127.0.0.1:5000/api/desktop/telemetry/status`.
`{"accepted": true}` means the path works. 403 `origin_refused` means Windows
traffic arrives with a non-loopback address in that configuration.

## 5. Ordering, duplicates and restarts

* **Heartbeats** are ordered per device by `(instance_id, seq)`.
  * A `seq` that is not higher than the last accepted one from the same process
    is ignored (`stale`).
  * Another process of the same device (the app was restarted) replaces the
    current one if its `instance_started_at` is later.
  * A heartbeat from an older process is ignored (`superseded`) until the newer
    one has been silent for `heartbeat_timeout_s`.
* **Sessions** are owned by the device that first reported them. A session
  reported live belongs to that desktop process, and another process can't
  continue it. "Reset Counters" on the desktop starts a new session id, so
  counters start from zero there.
* **Counters:**

| Kind | Values | Rule |
|---|---|---|
| Cumulative | `unique`, `crossings`, `line_totals`, `frames_processed` | May never decrease within a session (422) |
| Instantaneous | `visible`, the FPS values, `latency_ms`, `active_tracks`, `frames_dropped`, `recording_frames_dropped` | Stored as sent; may go up or down (the drop counters restart when a camera reconnects) |

* **Events** are idempotent: resending a page reports `duplicates` and stores
  nothing new. The same key with a different class or timestamp is refused
  (409 `event_conflict`) and the whole page is rolled back.
* **Summaries:**
  * A summary finalizes a session once. After that, the session, its counts and
    its events can't change: late heartbeats are ignored, new events are
    refused, an identical summary returns 200, and a different one returns 409.
  * Before finalizing, the stored events must match `crossing_event_count` and
    the per-class `crossings`.
  * With `stats_complete: true`, the summary may not be lower than the last
    live counts.
  * With `stats_complete: false`, the session keeps its last live `unique`,
    line totals and frame count (`counts_origin: "last_live_update"`), or
    `"incomplete"` if it was never live. Its crossing totals always come from
    the summary and the stored events.
* **Offline sessions:** a session can be uploaded after the fact, as event pages
  and then a summary, without any heartbeat.

## 6. Persistence

The new tables are in `backend/atcs.db`. `db.create_all()` creates them at
startup; existing tables are not changed.

| Table | Contents |
|---|---|
| `monitor_devices` | name, token SHA-256, created / revoked / last-seen times (server clock) |
| `desktop_status` | last accepted heartbeat per device: instance id and start, seq, `desktop_sent_at` (as sent) and its UTC copy, `received_at` (server), validated document |
| `desktop_sessions` | one row per session id: owning device, instance, source, desktop `started_at` / `ended_at` (as sent) and a UTC copy for sorting, status, counts, `counts_origin`, model, recording file names, canonical summary and its SHA-256, server `first_received_at` / `last_received_at` / `finalized_at` |
| `desktop_crossing_events` | events with the desktop timestamp as sent and the server `received_at`; unique on `(session_id, line_id, track_id, direction)` |

* Desktop timestamps are kept exactly as sent, including their offset. Server
  timestamps are separate UTC columns.
* Everything survives a restart of Flask, of the desktop or of the browser.
* Recreating `atcs.db` removes all devices and telemetry, like grants.
* The desktop's own SQLite database stays the source of truth. Flask never opens it.

## 7. Limits and known limitations

| Item | Default (`backend/config.py`) |
|---|---|
| Body size | status 8 KB, events 128 KB, summary 32 KB |
| Rate | 5 requests/s per device, bursts of 30 (`DESKTOP_RATE_PER_S`, `DESKTOP_RATE_BURST`) |
| Heartbeat timeout | 15 s (`DESKTOP_HEARTBEAT_TIMEOUT_S`) |
| Clock skew | 300 s (`DESKTOP_MAX_CLOCK_SKEW_S`) |
| Count bounds | counts ≤ 10,000,000; visible ≤ 10,000 per class; ≤ 16 counting lines; ≤ 100 recordings |

* Rate limits and the write lock are in memory, in one process. That matches
  the single-process `run.py`. Several worker processes would each have their
  own limits, and concurrent writes would rely on SQLite alone.
* There is no lockout after failed credentials. Every accepted client is on
  this computer, so a lockout would let any local program block the real
  desktop, and 256-bit tokens can't practically be guessed.
* Any local program that has a device's token can act as that device. Keep
  tokens in the user's own profile.
* Desktop-reported values are validated for shape, bounds, ownership, ordering
  and consistency, but they are still the desktop's claims. The backend can't
  tell whether the counts match what the camera saw.

## Tests

    cd backend && venv/bin/pytest tests/test_desktop_schema.py tests/test_desktop_telemetry.py

No desktop app, camera, monitor environment or network is needed.
