from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from api.cron_agenda import calendar_window, project_calendar


def window(start, days, zone="Europe/Amsterdam"):
    return calendar_window({"agenda_start": [start], "agenda_days": [str(days)], "agenda_zone": [zone]})


def job(id, schedule, first, **extra):
    return {"id": id, "owner_profile": "demo", "schedule": schedule, "next_run_at": first, **extra}


def test_project_only_remaining_runs_in_viewer_zone_and_exact_day_times():
    bounds = window("2026-11-15", 1)
    jobs = [job("interval", {"kind":"interval","minutes":60}, "2026-11-14T22:30:00+00:00",
                repeat={"times":5,"completed":1}),
            job("same", {"kind":"once"}, "2026-11-15T23:00:00+00:00"),
            job("paused", {"kind":"once"}, "2026-11-15T10:00:00+00:00", state="paused")]
    result = project_calendar(jobs, bounds, lambda _: None, datetime.fromisoformat("2026-11-14T00:00:00+00:00"))
    rows = result["days"]
    assert not result["partial"]
    assert len(rows) == 1 and rows[0]["id"] == "interval" and rows[0]["count"] == 3
    assert rows[0]["step_ms"] == 3600000


def test_cron_uses_owner_wall_clock_across_dst_and_preserves_duplicate_store_ids():
    jobs = [job("same", {"kind":"cron","expr":"0 9 * * *"}, "2026-10-24T09:00:00+02:00"),
            job("same", {"kind":"once"}, "2026-10-25T09:45:00+00:00", owner_profile="other")]
    result = project_calendar(jobs, window("2026-10-25", 1, "UTC"), lambda _: ZoneInfo("Europe/Amsterdam"),
                              datetime.fromisoformat("2026-10-24T00:00:00+00:00"))
    assert not result["partial"]
    assert [(r["owner"],datetime.fromtimestamp(r["first"]/1000,ZoneInfo("UTC")).hour) for r in result["days"]] == [("demo",8),("other",9)]
    assert result["days"][0]["times"] == [result["days"][0]["first"]]


def test_dense_cron_counts_bounds_and_no_history_invention():
    now = datetime.fromisoformat("2026-11-16T23:57:00+00:00")
    bounds = window("2026-11-16", 1, "UTC")
    result = project_calendar([job("dense", {"kind":"cron","expr":"* * * * *"}, "2026-01-01T10:00:00+00:00")],
                              bounds, lambda _: None, now)
    assert result["days"][0]["count"] == len(result["days"][0]["times"]) == 3
    assert not result["partial"]
    assert not project_calendar([job("old", {"kind":"once"},now.isoformat())],window("2026-11-15",1),lambda _:None,now)["days"]


@pytest.mark.parametrize(("days","zone"),[(0,"UTC"),(32,"UTC"),(1,"bad/zone")])
def test_reject_invalid_calendar_window(days, zone):
    with pytest.raises(ValueError):
        window("2026-11-15",days,zone)
