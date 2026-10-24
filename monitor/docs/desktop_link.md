# Desktop link: ATCS Monitor desktop → ATCS backend (Phase 5b)

The desktop link connects the original ATCS Monitor desktop app (PySide6, running natively on
Windows) to the Phase 5a telemetry backend ([desktop_telemetry.md](desktop_telemetry.md)). While
you monitor a camera or a video file in the desktop app, it sends the live status and counts to
ATCS, and every finished session is saved in the ATCS database. Video never leaves the computer.

```
browser ──atcs-monitor://open──> Windows ──> pythonw atcs_desktop_link.pyw "%1"
                                               │  accepts only atcs-monitor://open
                                               ▼
                                 original ATCS Monitor window (unchanged)
                                   engine snapshot + Bridge signals (read only)
                                               ▼
                                 desktop link sender thread ──device token──> Flask /api/desktop/telemetry/*
                                   outbox.sqlite3 (finished sessions until acknowledged)
```

The web page's **Open Desktop Monitor** button is Phase 5c; this phase provides everything behind it.

## 1. Two ways to run the app

| Started with | Network | Behaviour |
|---|---|---|
| `start.cmd` / `start.sh` / `python -m app.main` / the installed `ATCS Monitor` | none | Exactly as before (standalone). |
| `atcs_desktop_link.pyw` (by hand or through `atcs-monitor://open`) | only to the configured loopback backend | Same window and controls. If the link is set up (section 4), a small `ATCS: ...` label appears in the status bar and status/counts/sessions are sent. If it is not set up or is disabled, it runs standalone. |

The linked app never selects a source and never starts monitoring or recording on its own: the
launcher always starts it with no arguments. You choose the camera or video file and press
**Start** in the desktop window, as usual. Like the standalone app, it lists the cameras at startup
(and on **Find Cameras**) by opening each one for a single test frame, which is not kept; the
camera light may turn on for a moment.

## 2. What is sent

| Document | When | Content |
|---|---|---|
| `desktop_status` | every second while monitoring, every 10 s when idle | app state and message, device (cpu/cuda), source (camera name or video file name), camera status (`ok` / `reconnecting` / `error` / `none`), recording active + file name, the current session's id and start time, visible / unique / crossing counts per class, line totals, frames processed, capture / inference / recording FPS, latency, active tracks, dropped frames, last error |
| `crossing_events` | after a session ends, in pages of up to 500 events (and below 120 KB) | each crossing with its original timestamp, line, track id, class and direction |
| `session_summary` | after all events of the session were accepted | status (`completed` / `interrupted` / `error`), start and end times, model, duration, frames, average FPS, unique and crossing totals, line totals, recording file names, `crossing_event_count`, `stats_complete` |

Only bare names are sent: local paths in messages are reduced to the file name, and camera
sources (stored locally as `<index>:<name>`) are sent as the camera name. Documents are built
from the engine snapshot (live) and from the app's own SQLite history (finished sessions), which
stays the source of truth: the link reads it but never writes to it.

**Completeness.** `stats_complete` is `true` when the app saved the session's statistics and every
crossing event. After a crash (the app marks the session `interrupted` at its next start) the
statistics were never saved: the summary then has `stats_complete: false`, the crossing totals of
the events that were saved, and zero/null for unique counts, frames and duration. Nothing is
invented. The backend keeps the last live counts it received for such a session
(`counts_origin: "last_live_update"`).

## 3. Sender and outbox

All network I/O happens on one background thread, one request at a time; the window never waits
for the network. The GUI hands over a snapshot once a second (and on every state change) and
reports finished sessions.

* **Heartbeats** that cannot be delivered are dropped; the next one replaces them.
* **Finished sessions** go through a durable outbox, `desktop_link/outbox.sqlite3`. A session the link has
  seen running is recorded as `open`. When the app finishes it (or, after a crash, marks it interrupted at
  the next start), the link waits for its recordings to be validated (at most 2 minutes), then freezes the
  exact event list and summary (`pending`). Frozen documents never change, so a retry can never produce a
  conflicting summary.
* Upload order: event pages first, then the summary. Progress is saved after every page, so a restart
  resumes where it stopped.
* After the backend acknowledges the summary (201, or 200 `duplicate`), the payload is deleted and only a
  small `done` record stays, so the outbox does not grow with history.
* Sessions the link never saw (from before it was set up, or from the separate Phase 3 monitor service)
  are not uploaded.
* Requests are paced below the backend's limit (one heartbeat per second plus at most one upload request
  every 0.3 s).

| Backend answer | What the link does |
|---|---|
| 2xx | next page / session done |
| 401 `device_unauthorized` | status `ATCS: device token refused`; **everything pauses for 60 s**; nothing is marked failed; sessions wait for a working token |
| 403 `origin_refused` / `browser_refused`, 404, 405, 3xx | configuration problem: same pause; redirects are never followed |
| 429 `rate_limited` | everything pauses for `Retry-After` (1 to 300 s) |
| 5xx, 408, network error, timeout, 422 `clock_skew` | retried with backoff: 2 s doubling up to 5 min (±20 %), never given up; a successful heartbeat after an outage retries at once |
| 409 `events_incomplete` | the events are uploaded again (at most twice), then the summary |
| other 409 / 422 / 400 / 413 / 415 | the session is **rejected**: kept in the outbox with its documents and the reason, not retried automatically. `python -m app.desktop_link status` lists it, `retry` queues it again |

Duplicates are harmless: events are idempotent on the backend, and an identical summary is accepted as
`duplicate`. Every process uses a new `instance_id`, as the backend requires.

On close, the link tries for up to 3 s to send what is ready (the last heartbeat and finished sessions).
Anything not sent stays in the outbox and is sent at the next start of the linked app. Closing the app never
deletes any result.

## 4. Device credential

* The token comes only from the backend's local admin script (`monitor_access.py device add <name>`).
  It is not the browser JWT and not the Phase 4 monitor service secret.
* `python -m app.desktop_link setup` asks for it without echoing it (or reads it from standard input with
  `--token-stdin`) and stores it in `desktop_link/device_token`:
  * on Windows, encrypted with DPAPI for the current Windows user: another account, or a copy of the file
    on another computer, cannot decrypt it;
  * elsewhere, as a file readable by its owner only (`0600`); the link refuses a file other users can read.
* The token is never written to `desktop_link.json`, to logs, to error messages, or to the command output,
  and no browser code can read it. It is sent only in the `Authorization` header, only to a loopback address
  (`127.0.0.1`, `localhost` or `::1`; other addresses are refused when the configuration is saved and loaded).
  System proxies are ignored.
* A revoked or wrong token is handled like any 401: the link pauses and keeps the sessions; run `setup`
  again with a new token.
* The backend takes the device identity from the token, so a desktop can only report as its own device and
  cannot add to another device's sessions (409 `session_conflict`, kept as rejected).

## 5. The `atcs-monitor://` launcher

`scripts/windows/install_uri_handler.ps1` registers, for the current user only
(`HKEY_CURRENT_USER\Software\Classes\atcs-monitor`, no administrator rights), the fixed command

    "<monitor>\.venv-win\Scripts\pythonw.exe" "<monitor>\atcs_desktop_link.pyw" "%1"

* The script checks that `pythonw.exe`, `atcs_desktop_link.pyw` and `app\desktop_link\launcher.py` exist,
  refuses a checkout on a network or `\\wsl$` share, refuses paths containing `"` or `%`, and never replaces
  an `atcs-monitor:` handler it did not create (it marks its own with an `ATCSDesktopLink` value; `-Replace`
  re-points only its own registration). `-DryRun` shows what it would write.
* The launcher accepts exactly `atcs-monitor://open` or `atcs-monitor://open/` (scheme case-insensitive) as
  its only argument. Any query, fragment, path, port, user info, encoded character, extra argument or other
  scheme exits with code 2 before anything starts, and is noted in `desktop_link/launcher.log`. No value from
  the browser is ever used as a path, a program or a shell command, and nothing is downloaded.
* Browsers ask before opening an external `atcs-monitor:` link; that prompt stays the user's decision.
* `uninstall_uri_handler.ps1` removes the key, and only if the install script created it.

**Duplicate launches.** The linked app holds a per-user named mutex (`Local\ATCSMonitor.DesktopLink`; a lock
file on Linux). A second launch (another click) does not start a second app, engine or camera session: it
asks the running one, over a local socket that only accepts the exact message `activate`, to restore and raise
its window, then exits. If any other ATCS Monitor is running (for example the installed app, detected through
the `ATCSMonitor.Running` mutex every ATCS Monitor holds), the linked app does not start either; that
window is raised by its title instead. Nothing is ever closed or killed.
Windows may refuse to move a window to the front when another program has the focus; the taskbar button
then flashes instead (the launching process calls `AllowSetForegroundWindow` first, which normally allows it).

## 6. Where the files are

All link files are in `<data>\desktop_link\` (`python -m app.desktop_link where`):

| Run from | `<data>` |
|---|---|
| a development checkout (`C:\dev\...\monitor`) | `<checkout>\monitor\data` (git-ignored) |
| with `ATCS_MONITOR_HOME` set | `%ATCS_MONITOR_HOME%\data` |

| File | Contents |
|---|---|
| `desktop_link.json` | `enabled`, `backend_url`, `device_name` (no secrets) |
| `device_token` | the token (DPAPI-encrypted on Windows) |
| `outbox.sqlite3` | upload queue (section 3) |
| `launcher.log` | launch decisions (refused URIs, duplicate launches) |
| `instance.lock` | Linux/macOS single-instance lock |

The installed ATCS Monitor (`%LOCALAPPDATA%\Programs\ATCS Monitor`) and its data
(`%LOCALAPPDATA%\ATCS Monitor`: history, settings, recordings) are not used, read or changed by the link.
A development checkout keeps its own history, settings and recordings under its `monitor\` folder.

## 7. Windows setup (development checkout)

Run these in **Windows PowerShell**, not in WSL. Steps 3 and 10 change the Windows registry for your user;
run them only when you want to.

1. **Prepare a Windows copy and Python environment.** Clone the repository to a local drive (not
   `\\wsl$`), then create `.venv-win` with the existing setup script
   ([windows.md](windows.md#3-install)):

   ```powershell
   git clone https://github.com/Friasru/ATCS-Adaptive-Traffic-Control-System.git C:\dev\ATCS-Adaptive-Traffic-Control-System
   cd C:\dev\ATCS-Adaptive-Traffic-Control-System\monitor
   git switch alberto/desktop-link        # until it is merged
   ```
2. **Install the desktop Monitor dependencies:**
   `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` (add `-Cpu` for the small CPU-only PyTorch).
   It also downloads `models\yolo26n.pt`. Check the plain app once with `.\start.cmd`.
3. **Register the launcher** (writes `HKCU\Software\Classes\atcs-monitor`):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\install_uri_handler.ps1 -DryRun   # review
   powershell -ExecutionPolicy Bypass -File scripts\windows\install_uri_handler.ps1
   ```
4. **Create a device identity** on the backend, in WSL (the token is printed once):

   ```bash
   cd ~/ATCS-Adaptive-Traffic-Control-System/backend
   venv/bin/python monitor_access.py device add alberto-windows
   ```
5. **Store the token** on Windows (input is hidden; the backend address is the WSL Flask server):

   ```powershell
   .venv-win\Scripts\python.exe -m app.desktop_link setup --backend http://127.0.0.1:5000 --device alberto-windows
   ```
   Paste the token when asked. Do not put it in a file in the repository, a chat, or a browser.
6. **Start Flask** in WSL (`venv/bin/python run.py`, which listens on `127.0.0.1:5000`).
7. **Test connectivity:** `.venv-win\Scripts\python.exe -m app.desktop_link check` should print
   `OK: the backend accepted this device's token and network origin.` This probe stores nothing.
   `403 origin_refused` means Windows traffic does not reach WSL as loopback in your WSL network mode
   ([desktop_telemetry.md, section 4](desktop_telemetry.md#4-network-origins-and-windows-to-wsl2-connectivity)).
8. **Open the linked app and send a session:** run `Start-Process 'atcs-monitor://open'` (or open that link
   in the browser and confirm the prompt; or run `.venv-win\Scripts\pythonw.exe atcs_desktop_link.pyw`).
   The status bar shows `ATCS: connected`. Choose a camera or **Open Video File...**, press **Start**, then
   **Stop**. Clicking the link again only brings the window to the front.
9. **Check the saved results:** `python -m app.desktop_link status` counts the session as sent; in WSL,
   `GET /api/monitor/desktop/sessions` (signed in with a viewer or operator grant) lists it with its
   counts. Closing the desktop app keeps it there.
10. **Uninstall the test launcher:**
    `powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_uri_handler.ps1`.
    `python -m app.desktop_link forget` deletes the stored token and settings; revoke the device on the
    backend with `monitor_access.py device revoke alberto-windows`.

To stop linking without uninstalling: `python -m app.desktop_link disable` (and `enable` to resume).

## 8. Validation status

| Area | How it was checked |
|---|---|
| Documents vs. the backend validators (status, event pages, summaries; complete, interrupted, error, missing events, repeated events, paths, odd camera names) | unit-tested in `tests/test_desktop_link_contract.py` against `backend/app/services/desktop_schema.py`; `backend/tests/test_desktop_link_contract.py` validates the shared fixture `tests/data/desktop_link_documents.json` |
| Sender and outbox (ordering, pagination, backoff, 401/403/404/409/422/429/5xx, Retry-After, duplicates, restarts, crash recovery, close-time flush, GUI never blocked) | unit-tested with a scripted backend (`test_desktop_link_service.py`) |
| Real backend: live status, completed and crashed sessions, re-upload after restart, late heartbeat, invalid and revoked tokens, two-device isolation, backend restart mid-upload, rate limiting | integration-tested against Flask in a subprocess (`test_desktop_link_backend.py`, Linux) |
| Main window with the link (standalone/broken config, linked session from a video, recording state, responsiveness while the backend hangs, window activation from another process, activation message filtering, busy window on Windows) | integration-tested with pytest-qt offscreen and a scripted detector (`test_desktop_link_gui.py`, Linux and Windows) |
| URI parsing, launcher decisions, duplicate launch, missing configuration, CLI, token storage | unit-tested (`test_desktop_link_launcher.py`, `test_desktop_link_config.py`) |
| Windows: URI rejection through `atcs_desktop_link.pyw`, named-mutex single instance (including release after the holder is killed), detection of a running ATCS Monitor, DPAPI token storage, `setup`/`check` CLI, live heartbeat and session upload from Windows to Flask in WSL2 (default NAT mode, `127.0.0.1`) | Windows-native, with Windows Python 3.14 (standard library only, no PySide6) on 2026-10-24 |
| Windows, linked GUI: `atcs_desktop_link.pyw` started directly (no URI) in `.venv-win` (Python 3.14, PySide6 6.12, CUDA PyTorch); standalone without configuration, `ATCS: connected` with it; idle and live status; a completed video-file session with recording (CUDA) persisted by Flask in WSL2 and kept after a Flask restart; a second launch raising the running window; the Phase 5b tests | Windows-native, 2026-10-24, with a development data folder and a separate development backend database; camera probing was disabled for these runs, so no camera was opened |
| `install_uri_handler.ps1` / `uninstall_uri_handler.ps1` | Windows-native **dry runs** only: refusal of a `\\wsl.localhost` checkout, of a missing or wrong interpreter, generated command, nothing written |
| Registry installation, browser prompt and click-through, the window coming to the front while another program has the focus, a physical webcam session through the link | **not yet tested** (needs the registry change, step 3, and a webcam session started by hand) |
