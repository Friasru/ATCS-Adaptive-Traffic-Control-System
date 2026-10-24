"""Settings dialog. Values are validated by Settings.validate() before use."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QLabel, QMessageBox, QSpinBox, QVBoxLayout, QWidget,
)

from app import paths
from app.detection.classes import DISPLAY_NAMES, MONITORED_CLASSES
from app.settings import DEVICES, LOG_LEVELS, RESOLUTIONS, TRACKERS, Settings

IMGSZ_CHOICES = (320, 416, 480, 512, 640, 800, 960, 1280)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, running: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self._orig = settings
        self.result_settings: Settings | None = None

        form = QFormLayout()
        self.model = QComboBox()
        models = paths.available_models() or [settings.model_name]
        if settings.model_name not in models:
            models.insert(0, settings.model_name)
        self.model.addItems(models)
        self.model.setCurrentText(settings.model_name)
        form.addRow("Model (models/)", self.model)

        self.device = QComboBox()
        self.device.addItems([d.upper() if d != "auto" else "Auto" for d in DEVICES])
        self.device.setCurrentIndex(DEVICES.index(settings.device))
        form.addRow("Device", self.device)

        self.conf = QDoubleSpinBox(decimals=2, minimum=0.01, maximum=0.99, singleStep=0.05, value=settings.confidence)
        form.addRow("Confidence threshold", self.conf)
        self.iou = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=0.95, singleStep=0.05, value=settings.iou)
        form.addRow("NMS IoU", self.iou)

        self.imgsz = QComboBox()
        self.imgsz.addItem("Auto (960 on GPU, 640 on CPU)", 0)
        for v in IMGSZ_CHOICES:
            self.imgsz.addItem(str(v), v)
        self.imgsz.setCurrentIndex(max(0, self.imgsz.findData(settings.imgsz)))
        form.addRow("Inference size (px)", self.imgsz)

        self.max_fps = QDoubleSpinBox(decimals=0, minimum=0, maximum=120, value=settings.max_processing_fps)
        self.max_fps.setSpecialValueText("Unlimited")
        form.addRow("Max processed FPS (camera)", self.max_fps)

        self.resolution = QComboBox()
        self.resolution.addItems([f"{w} x {h}" for w, h in RESOLUTIONS])
        cur = (settings.camera_width, settings.camera_height)
        if cur not in RESOLUTIONS:
            self.resolution.addItem(f"{cur[0]} x {cur[1]}")
        self.resolution.setCurrentText(f"{cur[0]} x {cur[1]}")
        form.addRow("Camera resolution", self.resolution)

        self.tracker = QComboBox()
        self.tracker.addItems(TRACKERS)
        self.tracker.setCurrentText(settings.tracker)
        form.addRow("Tracker", self.tracker)

        self.min_hits = QSpinBox(minimum=1, maximum=30, value=settings.min_hits)
        form.addRow("Frames to confirm a track", self.min_hits)

        self.anchor = QComboBox()
        self.anchor.addItems(["center", "bottom_center"])
        self.anchor.setCurrentText(settings.anchor)
        form.addRow("Crossing anchor point", self.anchor)

        self.hyst = QDoubleSpinBox(decimals=3, minimum=0.0, maximum=0.2, singleStep=0.005, value=settings.hysteresis_frac)
        form.addRow("Line dead zone (fraction of diagonal)", self.hyst)

        self.travel = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=2.0, singleStep=0.05,
                                     value=settings.min_travel_frac)
        self.travel.setToolTip("How far an object must move across the line, as a fraction of its own size, "
                               "before the crossing counts. Stops large objects that sit or stand on the line "
                               "from being counted when their box drifts. 0 = off.")
        form.addRow("Crossing travel (fraction of object size)", self.travel)

        self.log_level = QComboBox()
        self.log_level.addItems(LOG_LEVELS)
        self.log_level.setCurrentText(settings.event_log_level)
        form.addRow("Event log detail", self.log_level)

        self.overlay = QCheckBox("Burn count overlay into video")
        self.overlay.setChecked(settings.recording_overlay)
        form.addRow("", self.overlay)

        classes = QGroupBox("Detected classes")
        grid = QGridLayout(classes)
        self.class_boxes: dict[str, QCheckBox] = {}
        for i, c in enumerate(MONITORED_CLASSES):
            cb = QCheckBox(DISPLAY_NAMES[c])
            cb.setChecked(c in settings.enabled_classes)
            self.class_boxes[c] = cb
            grid.addWidget(cb, i // 3, i % 3)

        lay = QVBoxLayout(self)
        top = QWidget()
        top.setLayout(form)
        lay.addWidget(top)
        lay.addWidget(classes)
        if running:
            note = QLabel("Model, device, inference size, camera resolution, tracker and confirmation frames "
                          "take effect the next time monitoring starts.")
            note.setWordWrap(True)
            lay.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def build(self) -> Settings:
        s = self._orig.copy()
        s.model_name = self.model.currentText()
        s.device = DEVICES[self.device.currentIndex()]
        s.confidence = round(self.conf.value(), 2)
        s.iou = round(self.iou.value(), 2)
        s.imgsz = int(self.imgsz.currentData())
        s.max_processing_fps = float(self.max_fps.value())
        w, h = (int(v) for v in self.resolution.currentText().split(" x "))
        s.camera_width, s.camera_height = w, h
        s.tracker = self.tracker.currentText()
        s.min_hits = int(self.min_hits.value())
        s.anchor = self.anchor.currentText()
        s.hysteresis_frac = round(self.hyst.value(), 3)
        s.min_travel_frac = round(self.travel.value(), 2)
        s.event_log_level = self.log_level.currentText()
        s.recording_overlay = self.overlay.isChecked()
        s.enabled_classes = [c for c in MONITORED_CLASSES if self.class_boxes[c].isChecked()]
        return s

    def _accept(self) -> None:
        s = self.build()
        try:
            s.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid settings", str(exc))
            return
        self.result_settings = s
        self.accept()
