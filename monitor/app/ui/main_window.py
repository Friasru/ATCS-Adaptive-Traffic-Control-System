"""Main window: live view, counts, status and controls."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from app import APP_NAME, __version__, paths
from app.camera.sources import VIDEO_EXTENSIONS, CameraInfo, CameraSource, SourceError, VideoFileSource, discover_cameras
from app.detection.classes import DISPLAY_NAMES, MONITORED_CLASSES
from app.detection.detector import ModelError, download_weights
from app.recording.recorder import RecordingResult
from app.services.monitoring import MonitoringEngine, SessionReport, State
from app.settings import Settings, save_settings
from app.storage.export import export_session_csv, export_session_json
from app.storage.repository import Repository
from app.tracking.counter import DIRECTIONS, CountingLine
from app.ui.camera_view import CameraView
from app.ui.history_dialog import HistoryDialog
from app.ui.settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

DIRECTION_LABELS = {"both": "Both directions", "a_to_b": "A -> B only", "b_to_a": "B -> A only"}


class Bridge(QObject):
    """Carries engine callbacks (worker threads) to the GUI thread."""

    stateChanged = Signal(object, str)
    errorRaised = Signal(str)
    recordingFinished = Signal(object)
    sessionFinished = Signal(object)
    camerasFound = Signal(object)
    downloadDone = Signal(str)  # empty string = success, else error


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, repo: Repository):
        super().__init__()
        self.settings = settings
        self.repo = repo
        self.bridge = Bridge()
        self.engine = MonitoringEngine(
            settings, repo,
            on_state=self.bridge.stateChanged.emit,
            on_error=self.bridge.errorRaised.emit,
            on_recording_finished=self.bridge.recordingFinished.emit,
            on_session_finished=self.bridge.sessionFinished.emit,
        )
        self.state = State.IDLE
        self._last_frame_key: tuple = ()
        self._closing = False
        self._error_box_open = False
        self._discovering = False

        self.setWindowTitle(APP_NAME)
        self._build_ui()
        self.bridge.stateChanged.connect(self._on_state)
        self.bridge.errorRaised.connect(self._on_error)
        self.bridge.recordingFinished.connect(self._on_recording_finished)
        self.bridge.sessionFinished.connect(self._on_session_finished)
        self.bridge.camerasFound.connect(self._on_cameras)
        self.bridge.downloadDone.connect(self._on_download_done)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(40)
        self._apply_line_to_view()
        self._update_controls()
        self.refresh_cameras()

    # ================================================================== UI
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # --- source row
        src = QHBoxLayout()
        src.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(260)
        self.source_combo.currentIndexChanged.connect(lambda _: self._update_controls())
        src.addWidget(self.source_combo, 1)
        self.refresh_btn = QPushButton("Find Cameras")
        self.refresh_btn.clicked.connect(self.refresh_cameras)
        self.open_btn = QPushButton("Open Video File...")
        self.open_btn.clicked.connect(self.choose_video)
        self.loop_cb = QCheckBox("Replay video")
        self.loop_cb.setToolTip("Restart the video file automatically when it ends (counts continue in the same session)")
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.open_settings)
        for w in (self.refresh_btn, self.open_btn, self.loop_cb, self.settings_btn):
            src.addWidget(w)
        root.addLayout(src)

        # --- video
        self.view = CameraView()
        self.view.lineChanged.connect(self._on_line_dragged)
        root.addWidget(self.view, 1)

        # --- counts
        counts = QFrame()
        counts.setFrameShape(QFrame.Shape.StyledPanel)
        grid = QGridLayout(counts)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(2)
        bold = QFont()
        bold.setBold(True)
        rows = (("visible", "Visible now"), ("unique", "Unique observed"), ("crossings", "Line crossings"))
        for c, cls in enumerate(MONITORED_CLASSES, start=1):
            h = QLabel(DISPLAY_NAMES[cls])
            h.setFont(bold)
            h.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(h, 0, c)
        self.count_labels: dict[str, dict[str, QLabel]] = {}
        for r, (key, title) in enumerate(rows, start=1):
            t = QLabel(title)
            t.setFont(bold)
            grid.addWidget(t, r, 0)
            self.count_labels[key] = {}
            for c, cls in enumerate(MONITORED_CLASSES, start=1):
                lab = QLabel("0")
                lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lab.setObjectName(f"count_{key}_{cls}")
                grid.addWidget(lab, r, c)
                self.count_labels[key][cls] = lab
        root.addWidget(counts)

        # --- status
        status = QFrame()
        status.setFrameShape(QFrame.Shape.StyledPanel)
        sl = QGridLayout(status)
        sl.setContentsMargins(6, 3, 6, 3)
        self.source_label = QLabel("Source: -")
        self.fps_label = QLabel("FPS: -")
        self.state_label = QLabel("Status: Idle")
        self.state_label.setFont(bold)
        self.device_label = QLabel("Device: -")
        self.rec_label = QLabel("")
        self.rec_label.setStyleSheet("color: #c00000; font-weight: bold;")
        self.rec_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        sl.addWidget(self.source_label, 0, 0)
        sl.addWidget(self.fps_label, 0, 1)
        sl.addWidget(self.state_label, 1, 0)
        sl.addWidget(self.device_label, 1, 1)
        sl.addWidget(self.rec_label, 2, 0, 1, 2)
        sl.setColumnStretch(0, 1)
        sl.setColumnStretch(1, 1)
        root.addWidget(status)

        # --- main controls
        ctl = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.record_btn = QPushButton("Record")
        self.end_rec_btn = QPushButton("End Recording")
        self.start_btn.clicked.connect(self.start_monitoring)
        self.stop_btn.clicked.connect(self.stop_monitoring)
        self.record_btn.clicked.connect(self.start_recording)
        self.end_rec_btn.clicked.connect(self.stop_recording)
        for b in (self.start_btn, self.stop_btn, self.record_btn, self.end_rec_btn):
            ctl.addWidget(b)
        root.addLayout(ctl)

        # --- lines / history / export
        aux = QHBoxLayout()
        self.lines_btn = QPushButton("Counting Lines")
        self.lines_btn.setCheckable(True)
        self.lines_btn.setToolTip("Edit the counting line by dragging its end points on the video")
        self.lines_btn.toggled.connect(self.view.set_edit_mode)
        self.line_enabled_cb = QCheckBox("Line enabled")
        self.line_enabled_cb.setChecked(self.settings.lines_enabled)
        self.line_enabled_cb.toggled.connect(self._on_line_options)
        self.direction_combo = QComboBox()
        for d in DIRECTIONS:
            self.direction_combo.addItem(DIRECTION_LABELS[d], d)
        self.direction_combo.setCurrentIndex(DIRECTIONS.index(self._line().direction))
        self.direction_combo.currentIndexChanged.connect(self._on_line_options)
        self.reset_btn = QPushButton("Reset Counters")
        self.reset_btn.clicked.connect(self.reset_counters)
        self.history_btn = QPushButton("History")
        self.history_btn.clicked.connect(self.open_history)
        self.export_btn = QPushButton("Export")
        self.export_btn.setToolTip("Export the most recent finished session to CSV and JSON")
        self.export_btn.clicked.connect(self.export_latest)
        for w in (self.lines_btn, self.line_enabled_cb, self.direction_combo, self.reset_btn):
            aux.addWidget(w)
        aux.addStretch(1)
        aux.addWidget(self.history_btn)
        aux.addWidget(self.export_btn)
        root.addLayout(aux)

        self.setCentralWidget(central)
        self.statusBar().showMessage(f"{APP_NAME} {__version__} - local processing only, nothing is uploaded")
        self.resize(980, 760)

    # ============================================================= sources
    def refresh_cameras(self) -> None:
        if self._discovering:
            return
        self._discovering = True
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Searching...")

        def work():
            try:
                cams = discover_cameras()
            except Exception:
                log.exception("Camera discovery failed")
                cams = []
            self.bridge.camerasFound.emit(cams)

        threading.Thread(target=work, name="camera-discovery", daemon=True).start()

    def _on_cameras(self, cams: list[CameraInfo]) -> None:
        self._discovering = False
        self.refresh_btn.setText("Find Cameras")
        current = self.source_combo.currentData()
        files = [self.source_combo.itemData(i) for i in range(self.source_combo.count())
                 if (self.source_combo.itemData(i) or ("",))[0] == "file"]
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for cam in cams:
            self.source_combo.addItem(f"Camera: {cam.label}", ("camera", cam.index, cam.name, cam.backend))
        if not cams:
            self.source_combo.addItem("No camera detected", None)
        for f in files:
            self.source_combo.addItem(f"Video file: {Path(f[1]).name}", f)
        idx = self.source_combo.findData(current) if current is not None else -1
        if idx < 0 and not cams and files:
            idx = self.source_combo.count() - 1
        self.source_combo.setCurrentIndex(max(0, idx))
        self.source_combo.blockSignals(False)
        self.statusBar().showMessage(f"{len(cams)} camera(s) found" if cams else
                                     "No camera found - you can open a video file instead", 6000)
        self._update_controls()

    def choose_video(self) -> None:
        pattern = " ".join(f"*{e}" for e in VIDEO_EXTENSIONS)
        media = paths.project_root() / "data" / "test_media"
        start = str(media if media.is_dir() else Path.home())
        fn, _ = QFileDialog.getOpenFileName(self, "Open video file", start, f"Video files ({pattern});;All files (*)")
        if fn:
            self.add_video_source(fn)

    def add_video_source(self, fn: str) -> None:
        data = ("file", str(Path(fn).resolve()))
        idx = self.source_combo.findData(data)
        if idx < 0:
            self.source_combo.addItem(f"Video file: {Path(fn).name}", data)
            idx = self.source_combo.count() - 1
        self.source_combo.setCurrentIndex(idx)

    def _make_source(self):
        data = self.source_combo.currentData()
        if not data:
            return None
        if data[0] == "camera":
            return CameraSource(int(data[1]), self.settings.camera_width, self.settings.camera_height, name=data[2],
                                backend=data[3])
        return VideoFileSource(data[1], realtime=True)

    # ============================================================ actions
    def start_monitoring(self) -> None:
        if self.engine.is_running or self.state in (State.INITIALIZING, State.STOPPING):
            return  # rapid clicks are ignored
        try:
            source = self._make_source()
        except SourceError as exc:
            self._on_error(str(exc))
            return
        if source is None:
            QMessageBox.information(self, "No source", "Select a camera or open a video file first.")
            return
        weights = paths.find_model(self.settings.model_name)
        if not weights.is_file():
            self._offer_download(weights.name)
            return
        self.view.clear("Starting...")
        self.engine.update_live_settings(self.settings)
        self.engine.start(source, loop=self.loop_cb.isChecked() and isinstance(source, VideoFileSource))
        self._update_controls()

    def stop_monitoring(self) -> None:
        if self.engine.is_running:
            self.engine.stop()
            self._on_state(State.STOPPING, "Stopping")

    def start_recording(self) -> None:
        if self.state == State.DETECTING:
            self.engine.start_recording()
            self.record_btn.setEnabled(False)

    def stop_recording(self) -> None:
        if self.state == State.RECORDING:
            self.engine.stop_recording()
            self.end_rec_btn.setEnabled(False)

    def reset_counters(self) -> None:
        if self.engine.is_running:
            ans = QMessageBox.question(
                self, "Reset counters",
                "The current counts will be saved as a finished session in History, and counting restarts "
                "from zero in a new session on the same source.\n\nContinue?")
            if ans == QMessageBox.StandardButton.Yes:
                self.engine.reset_counters()
        else:
            self._show_counts({k: {} for k in ("visible", "unique", "crossings")})

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self.engine.is_running, self)
        if dlg.exec() and dlg.result_settings is not None:
            self.settings = dlg.result_settings
            self._save_settings()
            if self.engine.is_running:
                self.engine.update_live_settings(self.settings)

    def open_history(self) -> None:
        HistoryDialog(self.repo, self).exec()

    def export_latest(self) -> None:
        current = self.engine.latest_snapshot().session_id if self.engine.is_running else None
        sessions = [s for s in self.repo.list_sessions(50) if s["status"] != "running" and s["session_id"] != current]
        if not sessions:
            QMessageBox.information(self, "Export", "There is no finished session to export yet. "
                                    "Stop monitoring (or use Reset Counters) to finish a session.")
            return
        s = sessions[0]
        try:
            out = paths.exports_dir()
            j = export_session_json(self.repo, s["session_id"], out / f"session_{s['session_id'][:8]}.json")
            c = export_session_csv(self.repo, s["session_id"], out)
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete",
                                f"Session started {s['started_at']}\nSaved:\n{j}\n{c[0]}\n{c[1]}")

    # ======================================================== model download
    def _offer_download(self, name: str) -> None:
        ans = QMessageBox.question(
            self, "Model weights missing",
            f"The detection model '{name}' is not in the models/ folder.\n\n"
            "Download the official file now? (needs an internet connection once)")
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.start_btn.setEnabled(False)
        self._on_state(State.INITIALIZING, f"Downloading {name}...")

        def work():
            os.environ["YOLO_OFFLINE"] = "false"
            try:
                download_weights(name)
                self.bridge.downloadDone.emit("")
            except ModelError as exc:
                self.bridge.downloadDone.emit(str(exc))
            finally:
                os.environ["YOLO_OFFLINE"] = "true"

        threading.Thread(target=work, name="model-download", daemon=True).start()

    def _on_download_done(self, error: str) -> None:
        if error:
            self._on_state(State.ERROR, error)
            self._on_error(error)
        else:
            self._on_state(State.IDLE, "Model downloaded")
            self.start_monitoring()

    # ======================================================= counting line
    def _line(self) -> CountingLine:
        lines = self.settings.counting_lines()
        return lines[0] if lines else CountingLine()

    def _apply_line_to_view(self) -> None:
        self.view.set_line(self._line())

    def _set_line(self, line: CountingLine) -> None:
        line.validate()
        self.settings.lines = [line.to_dict()]
        self._save_settings()
        self.engine.set_lines([line], enabled=self.settings.lines_enabled)
        self._apply_line_to_view()

    def _on_line_dragged(self, line: CountingLine) -> None:
        self._set_line(line)

    def _on_line_options(self) -> None:
        self.settings.lines_enabled = self.line_enabled_cb.isChecked()
        ln = self._line()
        ln.direction = self.direction_combo.currentData()
        self._set_line(ln)

    def _save_settings(self) -> None:
        try:
            save_settings(self.settings)
        except (OSError, ValueError) as exc:
            self.statusBar().showMessage(f"Could not save settings: {exc}", 8000)

    # =================================================== engine callbacks
    def _on_state(self, state: State, message: str) -> None:
        self.state = state
        text = f"Status: {state.value}"
        if message:
            text += f" - {message}"
        self.state_label.setText(text)
        self.state_label.setToolTip(message)
        if state == State.RECORDING:
            self.rec_label.setText(f"● REC  recordings/{message.replace('Recording to ', '')}" if message else "● REC")
        elif state in (State.DETECTING, State.IDLE, State.ERROR):
            self.rec_label.setText("")
        if state in (State.IDLE, State.ERROR) and not self.view.has_frame():
            self.view.clear(message or "Stopped")
        self._update_controls()

    def _on_error(self, message: str) -> None:
        self.statusBar().showMessage(f"Error: {message}", 15000)
        if self._closing or self._error_box_open:
            return
        self._error_box_open = True
        try:
            QMessageBox.warning(self, APP_NAME, message)
        finally:
            self._error_box_open = False

    def _on_recording_finished(self, res: RecordingResult) -> None:
        if res.ok:
            extra = f", {res.frames_dropped} frames dropped" if res.frames_dropped else ""
            self.statusBar().showMessage(
                f"Recording saved: {res.path} ({res.duration_s:.1f} s, {res.codec}/{res.container}, "
                f"{res.validation.get('decoded_frames', 0)} frames verified{extra})", 15000)
        # Failures are reported through on_error by the engine.

    def _on_session_finished(self, rep: SessionReport) -> None:
        self.statusBar().showMessage(
            f"Session saved ({rep.status}): {sum(rep.stats['crossings'].values())} crossings, "
            f"{sum(rep.stats['unique'].values())} unique objects", 10000)

    # ============================================================ refresh
    def _refresh(self) -> None:
        snap = self.engine.latest_snapshot()
        key = (snap.session_id, snap.frame_index, snap.frames_processed)
        if snap.frame is None or key == self._last_frame_key:
            return
        self._last_frame_key = key
        self.view.set_frame(snap.frame)
        self._show_counts({"visible": snap.visible, "unique": snap.unique, "crossings": snap.crossings})
        kind = "Camera" if snap.source_kind == "camera" else "Video file"
        name = snap.source_label.split(":", 1)[-1]
        self.source_label.setText(f"{kind}: {name}")
        rec = f" | Rec {snap.recording_fps:.1f}" if snap.recording_path else ""
        dropped = f" | dropped {snap.frames_dropped}" if snap.frames_dropped else ""
        self.fps_label.setText(f"FPS capture {snap.capture_fps:.1f} | inference {snap.inference_fps:.1f}{rec}"
                               f" | {snap.latency_ms:.0f} ms{dropped}")
        self.device_label.setText(f"Device: {snap.device.upper()} | tracks {snap.active_tracks}"
                                  + (f" | t={snap.media_time:.1f}s" if snap.source_kind == "video_file" else ""))
        if snap.recording_frames_dropped:
            self.rec_label.setText(f"● REC  recordings/{Path(snap.recording_path).name}  "
                                   f"({snap.recording_frames_dropped} frames dropped)")

    def _show_counts(self, data: dict) -> None:
        for key, labels in self.count_labels.items():
            vals = data.get(key, {})
            for cls, lab in labels.items():
                lab.setText(str(vals.get(cls, 0)))

    def _update_controls(self) -> None:
        st = self.state
        running = self.engine.is_running
        busy = st in (State.INITIALIZING, State.STOPPING)
        has_source = self.source_combo.currentData() is not None
        self.start_btn.setEnabled(not running and not busy and has_source)
        self.stop_btn.setEnabled(running and st != State.STOPPING)
        self.record_btn.setEnabled(running and st == State.DETECTING)
        self.end_rec_btn.setEnabled(running and st == State.RECORDING)
        for w in (self.source_combo, self.refresh_btn, self.open_btn, self.loop_cb):
            w.setEnabled(not running and not busy)
        if not self._discovering:
            self.refresh_btn.setEnabled(not running and not busy)

    # ============================================================== close
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._closing = True
        if self.engine.is_running:
            self.state_label.setText("Status: Stopping - finalizing recording and session...")
            self.engine.stop()
            deadline = time.monotonic() + 60
            while self.engine.is_running and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.02)
        self.engine.join(30)
        self.timer.stop()
        self._save_settings()
        event.accept()
