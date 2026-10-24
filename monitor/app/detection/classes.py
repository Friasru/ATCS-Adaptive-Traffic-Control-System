"""Monitored object classes and their mapping to the model's class ids."""

from __future__ import annotations

from typing import Mapping

# Order used everywhere in the UI, exports and the integration contract.
MONITORED_CLASSES: tuple[str, ...] = ("person", "car", "motorcycle", "bicycle", "bus", "truck")

DISPLAY_NAMES: dict[str, str] = {
    "person": "People",
    "car": "Cars",
    "motorcycle": "Motorcycles",
    "bicycle": "Bicycles",
    "bus": "Buses",
    "truck": "Trucks",
}

# Stable BGR colours per class (OpenCV order).
CLASS_COLORS_BGR: dict[str, tuple[int, int, int]] = {
    "person": (60, 180, 75),
    "car": (200, 130, 0),
    "motorcycle": (0, 140, 255),
    "bicycle": (180, 30, 145),
    "bus": (0, 200, 220),
    "truck": (40, 40, 220),
}


def zero_counts() -> dict[str, int]:
    return {c: 0 for c in MONITORED_CLASSES}


def resolve_class_ids(model_names: Mapping[int, str]) -> dict[str, int]:
    """Map monitored class names to the ids the loaded model actually uses.

    The mapping is read from the model instead of hard-coding COCO ids, and a
    model that lacks any monitored class is rejected.
    """
    by_name = {str(v).strip().lower(): int(k) for k, v in model_names.items()}
    missing = [c for c in MONITORED_CLASSES if c not in by_name]
    if missing:
        raise ValueError(f"Model is missing required classes: {', '.join(missing)}")
    return {c: by_name[c] for c in MONITORED_CLASSES}
