"""atcs-monitor:// launcher: URI parsing, argument checks, duplicate-launch handling."""

import pytest

from app.desktop_link import instance, launcher, uri
from app.desktop_link.config import link_dir


@pytest.mark.parametrize("value", ["atcs-monitor://open", "atcs-monitor://open/", "ATCS-Monitor://open"])
def test_accepted_uris(value):
    assert uri.parse(value) == "open"


@pytest.mark.parametrize("value", [
    "", " ", "atcs-monitor://", "atcs-monitor:open", "atcs-monitor:/open", "atcs-monitor:///open",
    "atcs-monitor://open?", "atcs-monitor://open?camera=1", "atcs-monitor://open/?start=1", "atcs-monitor://open#x",
    "atcs-monitor://open//", "atcs-monitor://open/x", "atcs-monitor://open/../../calc.exe", "atcs-monitor://OPEN",
    "atcs-monitor://start", "atcs-monitor://record", "atcs-monitor://open%2F", "atcs-monitor://user@open",
    "atcs-monitor://open:8080", "atcs-monitor://open ", " atcs-monitor://open", "atcs-monitor://open\n",
    "atcs-monitor://open\x00", "atcs-monitor://open&calc.exe", 'atcs-monitor://open" --start "',
    "atcs-monitor://open --video C:\\x.mp4", "atcs-monitor://open\\..\\x", "atcs-monitorx://open",
    "http://open", "file:///C:/Windows/System32/calc.exe", "C:\\Windows\\System32\\calc.exe", "--start",
    "--video=C:\\clip.mp4", "atcs-monitor://" + "o" * 100, "atcs-monitor://open/" + "/" * 60,
])
def test_rejected_uris(value):
    with pytest.raises(uri.LaunchRejected):
        uri.parse(value)


@pytest.mark.parametrize("value", [None, 1, b"atcs-monitor://open", ["atcs-monitor://open"]])
def test_rejected_types(value):
    with pytest.raises(uri.LaunchRejected):
        uri.parse(value)


@pytest.mark.parametrize("argv", [["atcs-monitor://open", "--start"], ["--start"], ["--video", "x.mp4"],
                                  ["atcs-monitor://open", "atcs-monitor://open"], ["--camera", "0"]])
def test_rejected_argument_lists(argv):
    with pytest.raises(uri.LaunchRejected):
        uri.check_argv(argv)


def test_accepted_argument_lists():
    assert uri.check_argv([]) == "open"
    assert uri.check_argv(["atcs-monitor://open/"]) == "open"


# ------------------------------------------------------------------ launcher
@pytest.fixture
def launch(home, monkeypatch):
    calls = {"app": [], "activate": 0, "focus": 0}

    def fake_app_main(argv, window_hook=None):
        calls["app"].append((list(argv), window_hook))
        return 0

    import app.main

    monkeypatch.setattr(app.main, "main", fake_app_main)
    monkeypatch.setattr(launcher, "activate_running", lambda: calls.__setitem__("activate", calls["activate"] + 1)
                        or True)
    monkeypatch.setattr(launcher, "focus_window_by_title", lambda: calls.__setitem__("focus", calls["focus"] + 1)
                        or True)
    monkeypatch.setattr(launcher, "original_app_running", lambda: False)
    return calls


def test_valid_uri_starts_app_with_fixed_arguments(launch):
    assert launcher.main(["atcs-monitor://open"]) == 0
    assert launch["app"] == [([], launcher.attach)]  # no source, no --start, no --record


def test_started_without_uri(launch):
    assert launcher.main([]) == 0
    assert launch["app"][0][0] == []


@pytest.mark.parametrize("argv", [["atcs-monitor://open?start=1"], ["atcs-monitor://open", "--start"],
                                  ["--video", "C:\\x.mp4", "--start"], ["file:///C:/calc.exe"]])
def test_invalid_launch_starts_nothing(launch, argv):
    assert launcher.main(argv) == 2
    assert launch["app"] == [] and launch["activate"] == 0
    log = (link_dir() / "launcher.log").read_text()
    assert "Launch refused" in log


def test_repeated_click_activates_running_instance(launch):
    held = instance.InstanceGuard()
    assert held.acquire()
    try:
        for _ in range(3):
            assert launcher.main(["atcs-monitor://open"]) == 0
        assert launch["app"] == [] and launch["activate"] == 3
    finally:
        held.release()
    assert launcher.main(["atcs-monitor://open"]) == 0
    assert len(launch["app"]) == 1


def test_running_instance_without_activation_falls_back_to_window_title(launch, monkeypatch):
    monkeypatch.setattr(launcher, "activate_running", lambda: False)
    held = instance.InstanceGuard()
    assert held.acquire()
    try:
        assert launcher.main(["atcs-monitor://open"]) == 0
    finally:
        held.release()
    assert launch["focus"] == 1 and launch["app"] == []


def test_other_atcs_monitor_running_is_not_duplicated(launch, monkeypatch):
    monkeypatch.setattr(launcher, "original_app_running", lambda: True)
    assert launcher.main(["atcs-monitor://open"]) == 0
    assert launch["app"] == [] and launch["focus"] == 1
    # the guard was released: a later launch works once the other app is closed
    monkeypatch.setattr(launcher, "original_app_running", lambda: False)
    assert launcher.main(["atcs-monitor://open"]) == 0
    assert len(launch["app"]) == 1


def test_instance_guard_is_exclusive(home):
    a, b = instance.InstanceGuard(), instance.InstanceGuard()
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_activation_name_is_per_user(monkeypatch):
    monkeypatch.setattr(instance.getpass, "getuser", lambda: "alice")
    a = instance.activation_name()
    monkeypatch.setattr(instance.getpass, "getuser", lambda: "bob")
    assert instance.activation_name() != a and a.startswith("atcs-monitor-link-")


def test_windows_helpers_are_inert_elsewhere():
    import sys

    if sys.platform == "win32":
        pytest.skip("checked natively on Windows")
    assert instance.original_app_running() is False
    assert instance.focus_window_by_title() is False
    instance.allow_foreground()


def test_pyw_entry_point_uses_launcher():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "atcs_desktop_link.pyw").read_text()
    assert "from app.desktop_link.launcher import main" in text
    assert "sys.exit(main(sys.argv[1:]))" in text
