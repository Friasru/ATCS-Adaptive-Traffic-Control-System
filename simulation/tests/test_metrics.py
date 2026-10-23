import pytest

import metrics


def write_tripinfo(path, trips):
    rows = "".join(
        f'<tripinfo id="v{i}" waitingTime="{w}" timeLoss="{t}" duration="{d}"/>'
        for i, (w, t, d) in enumerate(trips)
    )
    path.write_text(f"<tripinfos>{rows}</tripinfos>")
    return str(path)


def test_summarize_averages_and_maximum(tmp_path):
    path = write_tripinfo(tmp_path / "tripinfo.xml", [(2.0, 4.0, 30.0), (6.0, 8.0, 50.0)])
    stats = metrics.summarize(path)
    assert stats["vehicles_completed"] == 2
    assert stats["avg_wait_s"] == pytest.approx(4.0)
    assert stats["max_wait_s"] == pytest.approx(6.0)
    assert stats["avg_time_loss_s"] == pytest.approx(6.0)
    assert stats["avg_trip_duration_s"] == pytest.approx(40.0)


def test_summarize_single_trip(tmp_path):
    path = write_tripinfo(tmp_path / "tripinfo.xml", [(3.5, 7.0, 21.0)])
    stats = metrics.summarize(path)
    assert stats["vehicles_completed"] == 1
    assert stats["avg_wait_s"] == pytest.approx(3.5)
    assert stats["max_wait_s"] == pytest.approx(3.5)
