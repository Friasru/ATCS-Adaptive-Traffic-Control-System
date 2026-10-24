"""Desktop link: connects this desktop app to the ATCS backend (Phase 5b).

The link is an optional layer around the unchanged monitor. It is active only
when the app is started through the linked entry point (atcs_desktop_link.pyw,
also used by the atcs-monitor:// launcher) and a link configuration exists.
Otherwise the app runs exactly as before and nothing leaves the computer.

* uri        - the only accepted launcher URIs
* instance   - one linked app per user; activating the running one
* config     - backend address and the device token (never logged)
* documents  - desktop_status / crossing_events / session_summary documents
* client     - HTTP client for /api/desktop/telemetry/*
* outbox     - durable local queue of finished sessions
* service    - background sender (heartbeats + uploads)
* qt_link    - glue between the main window and the sender
* launcher   - entry point for atcs_desktop_link.pyw

See docs/desktop_link.md.
"""
