"""Glue between the unchanged main window and the background sender.

The link only listens: it connects extra slots to the window's existing Bridge
signals, reads engine.latest_snapshot() once a second, and adds one small label
to the status bar. It never starts, stops or changes monitoring or recording.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QLabel

from app import APP_NAME, __version__
from app.desktop_link.documents import RUNNING_STATES, LiveState
from app.desktop_link import instance
from app.desktop_link.service import LinkService
from app.storage.repository import iso

log = logging.getLogger("atcs_monitor.desktop_link")

LABELS = {
    "starting": "ATCS: connecting...",
    "connected": "ATCS: connected",
    "offline": "ATCS: offline - results are kept and sent later",
    "unauthorized": "ATCS: device token refused",
    "refused": "ATCS: connection refused by the backend",
    "incompatible": "ATCS: backend not found at the configured address",
    "rate_limited": "ATCS: busy, retrying",
    "clock": "ATCS: check the computer clock",
}


class DesktopLink(QObject):
    def __init__(self, window, service: LinkService, server_name: Optional[str] = None):
        super().__init__(window)
        self.window = window
        self.service = service
        self._message = ""
        self._last_error: Optional[str] = None
        self._finished: set[str] = set()

        bridge = window.bridge
        bridge.stateChanged.connect(self._on_state)
        bridge.errorRaised.connect(self._on_error)
        bridge.sessionFinished.connect(self._on_session_finished)
        bridge.recordingFinished.connect(lambda _res: self.service.poke())

        self.label = QLabel(LABELS["starting"])
        self.label.setObjectName("desktop_link_status")
        window.statusBar().addPermanentWidget(self.label)
        window.statusBar().showMessage(
            f"{APP_NAME} {__version__} - linked to ATCS ({service.config.backend_url}): counts and session results "
            "are sent, video never leaves this computer", 15000)

        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        name = server_name or instance.activation_name()
        if not self.server.listen(name):
            QLocalServer.removeServer(name)  # stale socket file left by a crash (Unix)
            if not self.server.listen(name):
                log.warning("Window activation is unavailable: %s", self.server.errorString())
        self.server.newConnection.connect(self._on_connection)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.tick()

    # ------------------------------------------------------------ signals
    def _on_state(self, state, message: str) -> None:
        self._message = message or ""
        if getattr(state, "value", state) == "Initializing":
            self._last_error = None
        self.tick()

    def _on_error(self, message: str) -> None:
        self._last_error = message

    def _on_session_finished(self, report) -> None:
        self._finished.add(report.session_id)
        self.service.session_finished(report.session_id)
        self.tick()

    # ------------------------------------------------------------- polling
    def live_state(self) -> LiveState:
        win = self.window
        engine = win.engine
        state = getattr(win.state, "value", str(win.state))
        snap = engine.latest_snapshot()
        running = engine.is_running and state in RUNNING_STATES
        sid = snap.session_id if running and snap.session_id and snap.session_id not in self._finished else None
        if state == "Error":
            camera = "error"
        elif not running:
            camera = "none"
        elif "reconnecting" in self._message.lower():
            camera = "reconnecting"
        else:
            camera = "ok"
        current = sid is not None  # counts belong to the running session only
        return LiveState(
            state=state, message=self._message, last_error=self._last_error, session_id=sid, timestamp=iso(),
            recording_active=state == "Recording", recording_file=(snap.recording_path or None) if current else None,
            camera_status=camera,
            visible=dict(snap.visible) if current else {}, unique=dict(snap.unique) if current else {},
            crossings=dict(snap.crossings) if current else {},
            line_totals={k: {d: dict(v) for d, v in t.items()} for k, t in snap.line_totals.items()} if current else {},
            frames_processed=snap.frames_processed if current else 0,
            capture_fps=snap.capture_fps, inference_fps=snap.inference_fps, recording_fps=snap.recording_fps,
            latency_ms=snap.latency_ms, frames_dropped=snap.frames_dropped,
            recording_frames_dropped=snap.recording_frames_dropped, active_tracks=snap.active_tracks,
            device=snap.device or None,
        )

    def tick(self) -> None:
        self.service.set_live(self.live_state())
        st = self.service.status()
        text = LABELS.get(str(st["connection"]), f"ATCS: {st['connection']}")
        waiting = st.get("pending", 0)
        if waiting:
            text += f" ({waiting} session{'s' if waiting != 1 else ''} to send)"
        if st.get("rejected"):
            text += f" - {st['rejected']} refused"
        self.label.setText(text)
        tip = [f"Backend: {self.service.config.backend_url}", f"Device: {self.service.config.device_name}"]
        if st.get("detail"):
            tip.append(st["detail"])
        if st.get("last_ok"):
            tip.append(f"Last contact: {st['last_ok']}")
        self.label.setToolTip("\n".join(tip))

    # ---------------------------------------------------------- activation
    def _on_connection(self) -> None:
        while self.server.hasPendingConnections():
            sock: QLocalSocket = self.server.nextPendingConnection()
            sock.readyRead.connect(lambda s=sock: self._on_request(s))
            sock.disconnected.connect(sock.deleteLater)
            timeout = QTimer(sock)  # deleted together with the socket, so it never fires on a dead one
            timeout.setSingleShot(True)
            timeout.timeout.connect(lambda s=sock: s.abort())
            timeout.start(2000)
            # On Windows (named pipes) the request may already be buffered when the connection is
            # accepted; readyRead was then emitted before it was connected and does not come again.
            if sock.bytesAvailable():
                self._on_request(sock)

    def _on_request(self, sock: QLocalSocket) -> None:
        data = bytes(sock.read(64))
        sock.disconnectFromServer()
        if data == instance.ACTIVATE:
            log.info("Another launch asked for this window; bringing it to the front")
            self.bring_to_front()

    def bring_to_front(self) -> None:
        win = self.window
        if win.isMinimized():
            win.showNormal()
        win.show()
        win.raise_()
        win.activateWindow()
        QApplication.alert(win)  # flashes the taskbar button if Windows refuses the focus change

    def close(self) -> None:
        self.timer.stop()
        self.server.close()
