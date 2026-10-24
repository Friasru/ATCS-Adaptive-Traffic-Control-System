"""Session history: saved sessions, their real counts, recordings and export."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSlider,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from app import paths
from app.detection.classes import DISPLAY_NAMES, MONITORED_CLASSES
from app.storage.export import export_session_csv, export_session_json
from app.storage.repository import Repository
from app.ui.camera_view import CameraView

COLUMNS = ("Started", "Duration", "Source", "Status", "Unique", "Crossings", "Recording")


def fmt_time(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return ""
    try:
        return datetime.fromisoformat(iso_ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso_ts


def fmt_duration(seconds) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class PlayerDialog(QDialog):
    """Minimal built-in player so recordings can be reviewed without relying
    on an external program being installed."""

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Recording - {path.name}")
        self.resize(900, 560)
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise OSError(f"Cannot decode {path.name}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 15.0
        self.total = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.view = CameraView()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, max(0, self.total - 1))
        self.slider.sliderMoved.connect(self._seek)
        self.play_btn = QPushButton("Pause")
        self.play_btn.clicked.connect(self._toggle)
        self.pos_label = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.play_btn)
        row.addWidget(self.slider, 1)
        row.addWidget(self.pos_label)
        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._next)
        self.timer.start(int(1000 / self.fps))

    def _next(self) -> None:
        ok, img = self.cap.read()
        if not ok:
            self.timer.stop()
            self.play_btn.setText("Play")
            return
        idx = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
        self.view.set_frame(img)
        self.slider.blockSignals(True)
        self.slider.setValue(idx)
        self.slider.blockSignals(False)
        self.pos_label.setText(f"{idx / self.fps:6.1f} s / {self.total / self.fps:.1f} s")

    def _toggle(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.play_btn.setText("Play")
        else:
            if self.slider.value() >= self.total - 1:
                self._seek(0)
            self.timer.start(int(1000 / self.fps))
            self.play_btn.setText("Pause")

    def _seek(self, frame: int) -> None:
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        self._next()

    def done(self, r) -> None:
        self.timer.stop()
        self.cap.release()
        super().done(r)


class HistoryDialog(QDialog):
    def __init__(self, repo: Repository, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Session history")
        self.resize(1000, 560)
        self.sessions: list[dict] = []

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._show_details)
        self.details = QPlainTextEdit(readOnly=True)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.details)
        split.setSizes([330, 200])

        self.play_btn = QPushButton("View Recording")
        self.folder_btn = QPushButton("Open Recordings Folder")
        self.csv_btn = QPushButton("Export CSV")
        self.json_btn = QPushButton("Export JSON")
        refresh = QPushButton("Refresh")
        close = QPushButton("Close")
        self.play_btn.clicked.connect(self.view_recording)
        self.folder_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.recordings_dir()))))
        self.csv_btn.clicked.connect(lambda: self.export("csv"))
        self.json_btn.clicked.connect(lambda: self.export("json"))
        refresh.clicked.connect(self.reload)
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        for b in (self.play_btn, self.folder_btn, self.csv_btn, self.json_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(refresh)
        row.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addLayout(row)
        self.reload()

    def reload(self) -> None:
        self.sessions = self.repo.list_sessions()
        self.table.setRowCount(len(self.sessions))
        for r, s in enumerate(self.sessions):
            rec = s["recording_paths"]
            vals = (
                fmt_time(s["started_at"]),
                fmt_duration(s.get("duration_s")),
                f"{'Camera' if s['source_type'] == 'camera' else 'Video file'}: {s['source_id']}",
                s["status"],
                str(sum((s.get("unique") or {}).values())) if s.get("unique") is not None else "",
                str(sum((s.get("crossings") or {}).values())) if s.get("crossings") is not None else "",
                ", ".join(Path(p).name for p in rec) if rec else "-",
            )
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
        self.table.resizeColumnsToContents()
        if self.sessions:
            self.table.selectRow(0)
        else:
            self.details.setPlainText("No sessions recorded yet.")
        self._update_buttons()

    def selected(self) -> Optional[dict]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.sessions[rows[0].row()]

    def _update_buttons(self) -> None:
        s = self.selected()
        for b in (self.play_btn, self.csv_btn, self.json_btn):
            b.setEnabled(s is not None)

    def _show_details(self) -> None:
        self._update_buttons()
        s = self.selected()
        if s is None:
            return
        full = self.repo.get_session(s["session_id"]) or {}
        st = full.get("stats") or {}
        lines = [
            f"Session {s['session_id']}",
            f"Source: {s['source_type']} - {s['source_id']}",
            f"Started: {fmt_time(s['started_at'])}   Ended: {fmt_time(s.get('ended_at'))}   Status: {s['status']}",
            f"Model: {full.get('model_version') or '-'} on {full.get('device') or '-'}",
        ]
        if full.get("error"):
            lines.append(f"Error: {full['error']}")
        if st:
            lines.append(f"Frames processed: {st['frames_processed']}   Avg inference FPS: {st.get('avg_inference_fps')}"
                         f"   Peak visible: {st.get('peak_visible')}")
            lines.append("")
            lines.append(f"{'Class':<12}{'Unique':>8}{'Crossings':>11}")
            for c in MONITORED_CLASSES:
                lines.append(f"{DISPLAY_NAMES[c]:<12}{st['unique'].get(c, 0):>8}{st['crossings'].get(c, 0):>11}")
            for lid, t in (st.get("line_totals") or {}).items():
                lines.append(f"{lid}: A->B {sum(t['a_to_b'].values())}, B->A {sum(t['b_to_a'].values())}")
        else:
            lines.append("No statistics saved for this session.")
        for r in full.get("recordings", []):
            exists = Path(r["path"]).is_file()
            state = "OK" if r["ok"] else f"FAILED ({r.get('error') or 'not finalized'})"
            lines.append(f"Recording: {r['path']}  [{state}]{'' if exists else '  (file missing)'}")
        self.details.setPlainText("\n".join(lines))

    def view_recording(self) -> None:
        s = self.selected()
        if s is None:
            return
        full = self.repo.get_session(s["session_id"]) or {}
        recs = [r for r in full.get("recordings", []) if Path(r["path"]).is_file()]
        if not recs:
            QMessageBox.information(self, "No recording", "This session has no recording file on disk "
                                    "(none was made, or the file was moved or deleted).")
            return
        try:
            PlayerDialog(Path(recs[-1]["path"]), self).exec()
        except OSError as exc:
            QMessageBox.warning(self, "Cannot play recording", str(exc))

    def export(self, kind: str) -> None:
        s = self.selected()
        if s is None:
            return
        try:
            out = paths.exports_dir()
            if kind == "json":
                files = [export_session_json(self.repo, s["session_id"], out / f"session_{s['session_id'][:8]}.json")]
            else:
                files = list(export_session_csv(self.repo, s["session_id"], out))
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", "Saved:\n" + "\n".join(str(f) for f in files))
