"""GUI tests (pytest-qt, offscreen platform)."""

import time
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from app.services.monitoring import State
from app.settings import Settings, load_settings
from app.storage.repository import Repository
from app.tracking.counter import CountingLine
from app.ui.history_dialog import HistoryDialog, PlayerDialog
from app.ui.main_window import MainWindow
from app.ui.settings_dialog import SettingsDialog
from tests.conftest import DEFAULT_MODEL, require_media

pytestmark = pytest.mark.gui


@pytest.fixture
def boxes(monkeypatch):
    """Record message boxes instead of blocking on them."""
    seen = []

    def rec(kind, answer=None):
        def f(parent, title, text, *a, **k):
            seen.append((kind, title, text))
            return answer

        return f

    monkeypatch.setattr(QMessageBox, "information", rec("info"))
    monkeypatch.setattr(QMessageBox, "warning", rec("warning"))
    monkeypatch.setattr(QMessageBox, "critical", rec("critical"))
    monkeypatch.setattr(QMessageBox, "question", rec("question", QMessageBox.StandardButton.Yes))
    return seen


@pytest.fixture
def window(qtbot, home):
    (home / "data").mkdir(exist_ok=True)
    repo = Repository(home / "data" / "gui.sqlite3")
    w = MainWindow(Settings(model_name=DEFAULT_MODEL), repo)
    qtbot.addWidget(w)
    w.show()
    qtbot.waitUntil(lambda: not w._discovering, timeout=15000)
    yield w
    if w.engine.is_running:
        w.engine.stop(wait=True)
    w.engine.join()
    repo.close()


@pytest.fixture
def model_window(qtbot, model_home):
    (model_home / "data").mkdir(exist_ok=True)
    repo = Repository(model_home / "data" / "gui.sqlite3")
    w = MainWindow(Settings(model_name=DEFAULT_MODEL), repo)
    qtbot.addWidget(w)
    w.show()
    qtbot.waitUntil(lambda: not w._discovering, timeout=15000)
    yield w
    if w.engine.is_running:
        w.engine.stop(wait=True)
    w.engine.join()
    repo.close()


def test_window_launches_with_controls(window):
    assert window.windowTitle() == "ATCS Monitor"
    assert window.isVisible()
    for name in ("start_btn", "stop_btn", "record_btn", "end_rec_btn", "lines_btn", "history_btn", "export_btn",
                 "settings_btn"):
        assert getattr(window, name).isVisible()
    assert window.state_label.text().startswith("Status: Idle")
    assert all(l.text() == "0" for row in window.count_labels.values() for l in row.values())
    # Nothing can be recorded or stopped before monitoring starts.
    assert not window.stop_btn.isEnabled() and not window.record_btn.isEnabled()
    assert not window.end_rec_btn.isEnabled()


def test_camera_discovery_lists_sources(window):
    texts = [window.source_combo.itemText(i) for i in range(window.source_combo.count())]
    assert texts  # "No camera detected" when nothing is attached
    if texts == ["No camera detected"]:
        assert not window.start_btn.isEnabled()


def test_start_without_source_is_blocked(window, boxes):
    # Force "no source" even on machines where discovery found a real camera.
    window.source_combo.clear()
    window.source_combo.addItem("No camera detected", None)
    window.start_btn.setEnabled(True)
    window.start_monitoring()
    assert boxes and boxes[-1][0] == "info"
    assert not window.engine.is_running


def test_missing_model_offers_download(window, monkeypatch, tmp_path):
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda p, title, *a, **k: asked.append(title) or QMessageBox.StandardButton.No)
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x")
    window.add_video_source(str(video))
    window.start_monitoring()  # models/ is empty in this isolated home
    assert asked == ["Model weights missing"]
    assert not window.engine.is_running


def test_bad_video_file_shows_error(model_window, qtbot, boxes, tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video" * 200)
    model_window.add_video_source(str(bad))
    model_window.start_btn.click()
    qtbot.waitUntil(lambda: model_window.state == State.ERROR, timeout=60000)
    assert any(k == "warning" and "decode" in t for k, _, t in boxes)
    assert model_window.start_btn.isEnabled()  # user can recover


def test_settings_dialog_validates(qtbot, boxes):
    dlg = SettingsDialog(Settings(), running=False)
    qtbot.addWidget(dlg)
    for cb in dlg.class_boxes.values():
        cb.setChecked(False)
    dlg._accept()
    assert dlg.result_settings is None
    assert boxes[-1][0] == "warning" and "class" in boxes[-1][2]
    dlg.class_boxes["car"].setChecked(True)
    dlg.conf.setValue(0.5)
    dlg._accept()
    assert dlg.result_settings.enabled_classes == ["car"] and dlg.result_settings.confidence == 0.5


def test_counting_line_drag_updates_and_persists(window, qtbot):
    window.resize(800, 600)
    window.view.set_frame(np.zeros((480, 640, 3), np.uint8))
    window.lines_btn.setChecked(True)
    assert window.view.edit_mode
    v = window.view
    start = v.to_widget(window._line().p1).toPoint()
    target = v.to_widget((0.2, 0.2)).toPoint()
    qtbot.mousePress(v, Qt.MouseButton.LeftButton, pos=start)
    QApplication.sendEvent(v, QMouseEvent(QEvent.Type.MouseMove, QPointF(target), QPointF(v.mapToGlobal(target)),
                                          Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                          Qt.KeyboardModifier.NoModifier))
    qtbot.mouseRelease(v, Qt.MouseButton.LeftButton, pos=target)
    p1 = window._line().p1
    assert p1 == pytest.approx((0.2, 0.2), abs=0.02)
    assert load_settings().lines[0]["p1"] == pytest.approx([0.2, 0.2], abs=0.02)  # saved to disk
    window.direction_combo.setCurrentIndex(1)
    assert load_settings().lines[0]["direction"] == "a_to_b"


def test_degenerate_line_drag_rejected(window, qtbot):
    window.view.set_frame(np.zeros((480, 640, 3), np.uint8))
    window.lines_btn.setChecked(True)
    before = window._line()
    window.view._line = CountingLine("line-1", (0.5, 0.5), (0.5, 0.5))
    window.view._drag = 0
    window.view.mouseReleaseEvent(None)
    assert window._line() == before


@pytest.mark.media
def test_full_gui_session_with_recording_history_and_export(model_window, qtbot, boxes, model_home):
    w = model_window
    w.add_video_source(str(require_media("person-bicycle-car-detection.mp4")))
    # Rapid clicking must not start two pipelines.
    for _ in range(5):
        w.start_btn.click()
    qtbot.waitUntil(lambda: w.state == State.DETECTING, timeout=60000)
    assert w.engine.is_running

    # UI responsiveness during inference: measure event-loop gaps.
    gaps, last = [], [time.perf_counter()]

    def beat():
        now = time.perf_counter()
        gaps.append(now - last[0])
        last[0] = now

    t = QTimer()
    t.timeout.connect(beat)
    t.start(20)
    w.record_btn.click()
    qtbot.waitUntil(lambda: w.state == State.RECORDING, timeout=10000)
    assert "REC" in w.rec_label.text()
    qtbot.wait(4000)
    t.stop()
    assert max(gaps) < 0.25, f"UI blocked for {max(gaps):.3f}s"

    qtbot.waitUntil(lambda: int(w.count_labels["unique"]["person"].text()) + int(w.count_labels["unique"]["car"].text()) > 0
                    or w.engine.latest_snapshot().media_time > 20, timeout=60000)
    w.end_rec_btn.click()
    qtbot.waitUntil(lambda: w.engine.last_recording is not None, timeout=30000)
    assert w.engine.last_recording.ok
    w.stop_btn.click()
    qtbot.waitUntil(lambda: not w.engine.is_running and w.state == State.IDLE, timeout=30000)
    snap = w.engine.latest_snapshot()
    rep = w.engine.last_report
    # The labels show exactly what the engine counted.
    for cls, lab in w.count_labels["crossings"].items():
        assert int(lab.text()) == snap.crossings[cls] == rep.stats["crossings"][cls]

    w.export_btn.click()
    assert boxes[-1][0] == "info" and "Saved" in boxes[-1][2]
    assert list((model_home / "exports").glob("session_*.json"))

    dlg = HistoryDialog(w.repo, w)
    qtbot.addWidget(dlg)
    assert dlg.table.rowCount() == 1
    assert "completed" in dlg.details.toPlainText()
    assert dlg.play_btn.isEnabled()
    player = PlayerDialog(Path(w.repo.get_session(rep.session_id)["recordings"][0]["path"]))
    qtbot.addWidget(player)
    qtbot.wait(300)
    assert player.view.has_frame()
    player.done(0)
    dlg.export("csv")
    assert boxes[-1][0] == "info"


@pytest.mark.media
def test_close_while_recording_finalizes(model_window, qtbot, model_home):
    w = model_window
    w.add_video_source(str(require_media("street_slovenska.mp4")))
    w.start_btn.click()
    qtbot.waitUntil(lambda: w.state == State.DETECTING, timeout=60000)
    w.record_btn.click()
    qtbot.waitUntil(lambda: w.state == State.RECORDING, timeout=10000)
    qtbot.wait(2000)
    w.close()
    assert not w.engine.is_running
    sess = w.repo.list_sessions()[0]
    assert sess["status"] == "completed"
    rec = w.repo.get_session(sess["session_id"])["recordings"][0]
    assert rec["ok"] and rec["frames"] > 0
