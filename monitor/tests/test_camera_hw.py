"""Physical-camera tests. Opt-in: set ATCS_TEST_CAMERA=1 (optionally ATCS_TEST_CAMERA_INDEX)."""

import os
import sys

import pytest

from app.camera.sources import CameraSource, SourceError, camera_backends, discover_cameras

pytestmark = [
    pytest.mark.camera,
    pytest.mark.skipif(os.environ.get("ATCS_TEST_CAMERA") != "1", reason="set ATCS_TEST_CAMERA=1 to use a real camera"),
]


@pytest.fixture(scope="module")
def cam():
    cams = discover_cameras()
    assert cams, "ATCS_TEST_CAMERA=1 but no camera was discovered"
    want = os.environ.get("ATCS_TEST_CAMERA_INDEX")
    if want is not None:
        cams = [c for c in cams if c.index == int(want)]
        assert cams, f"camera #{want} not discovered"
    return cams[0]


def test_discovered_camera_uses_platform_backend(cam):
    assert cam.backend in camera_backends()
    if sys.platform == "win32":
        assert not cam.name.startswith("Camera "), "DirectShow device name lookup failed"


@pytest.mark.parametrize("size", [(640, 480), (1280, 720)])
def test_capture_frames(cam, size):
    src = CameraSource(cam.index, *size, name=cam.name, backend=cam.backend)
    src.open()
    try:
        frames = [src.read() for _ in range(30)]
    finally:
        src.close()
    assert all(f is not None for f in frames)
    shapes = {f.image.shape for f in frames}
    assert len(shapes) == 1 and next(iter(shapes))[2] == 3
    assert [f.index for f in frames] == list(range(30))
    assert frames[-1].media_time > frames[0].media_time


def test_reopen_after_close_and_auto_backend(cam):
    src = CameraSource(cam.index, name=cam.name)  # backend=None: tries camera_backends() in order
    for _ in range(2):  # the engine's reconnect path re-opens the same object
        src.open()
        assert src.read() is not None
        src.close()
    assert src.backend in camera_backends()


def test_camera_busy_or_missing_reports_source_error(cam):
    with pytest.raises(SourceError):
        CameraSource(cam.index + 40, backend=cam.backend).open()
