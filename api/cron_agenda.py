"""Read-only calendar projections for already authorized scheduler rows."""
from datetime import date, datetime, time, timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


def calendar_window(query):
    try:
        start = date.fromisoformat(query["agenda_start"][0])
        days = int(query["agenda_days"][0])
        zone = ZoneInfo(query["agenda_zone"][0])
        if not 1 <= days <= 31 or not 1971 <= start.year <= 2099:
            raise ValueError
    except (ValueError, KeyError, IndexError, ZoneInfoNotFoundError):
        raise ValueError("Invalid agenda date, time zone or period.") from None
    return [datetime.combine(start + timedelta(days=n), time.min, zone) for n in range(days + 1)]


def _stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timezone missing")
    return parsed


def project_calendar(jobs, bounds, profile_zone, now=None):
    """One row per job/day. Calendar and repeat boundaries are exclusive at the end."""
    now = (now or datetime.now(UTC)).timestamp()
    windows = [(max(now, a.timestamp()), b.timestamp(), a.date().isoformat())
               for a, b in zip(bounds, bounds[1:])]
    end = windows[-1][1]
    day_detail = len(windows) == 1
    rows = []
    incomplete = False
    zones = {}
    # Bound work on imported schedules. A truncated projection is explicit.
    budget = 24000
    for job in jobs:
        if job.get("enabled") is False or job.get("state") in {"paused", "completed", "error"} or end <= now:
            continue
        try:
            if not job.get("next_run_at"):
                incomplete = True
                continue
            first = _stamp(job["next_run_at"])
            stamp = first.timestamp()
            if stamp >= end:
                continue
            schedule = job.get("schedule") or {}
            kind = schedule.get("kind")
            repeat = job.get("repeat") or {}
            total = repeat.get("times")
            left = None if total is None else max(0, int(total) - int(repeat.get("completed", 0)))
            if left == 0:
                continue
            owner = job["owner_profile"]
            def add(day, start, count, step=None):
                rows.append({"id": job["id"], "owner": owner, "day": day,
                             "first": int(start * 1000), "count": count})
                if day_detail and count:
                    if step:
                        rows[-1]["step_ms"] = step * 1000
                    else:
                        rows[-1]["times"] = [rows[-1]["first"]]
            if kind in {"interval", "once"}:
                step = int(schedule["minutes"]) * 60 if kind == "interval" else 1
                if step <= 0:
                    raise ValueError
                if kind == "once":
                    left = 1
                for a, b, day in windows:
                    lo = max(0, ceil((a - stamp) / step))
                    hi = ceil((b - stamp) / step)
                    if left is not None:
                        hi = min(hi, left)
                    if hi > lo:
                        add(day, stamp + lo * step, hi - lo, step)
                continue
            if kind != "cron":
                raise ValueError
            from croniter import croniter
            if owner not in zones:
                zones[owner] = profile_zone(owner)
            zone = zones[owner] or first.tzinfo
            limit = max(now, windows[0][0])
            # Unlimited cron expressions can seek straight to the requested window.
            seek = stamp < limit and left is None
            base = datetime.fromtimestamp(limit - 0.000001 if seek else stamp, zone)
            iterator = croniter(schedule["expr"], base.replace(tzinfo=None), max_years_between_matches=8)
            index = 0
            per_job = 4096
            while stamp < end and (left is None or left > 0):
                if not seek:
                    while index < len(windows) and stamp >= windows[index][1]:
                        index += 1
                    if index < len(windows) and stamp >= windows[index][0]:
                        day = windows[index][2]
                        if rows and (rows[-1]["owner"], rows[-1]["id"], rows[-1]["day"]) == (owner, job["id"], day):
                            rows[-1]["count"] += 1
                            if day_detail:
                                rows[-1]["times"].append(int(stamp * 1000))
                        else:
                            add(day, stamp, 1)
                    if left is not None:
                        left -= 1
                        if left == 0:
                            break
                seek = False
                if per_job <= 0 or budget <= 0:
                    incomplete = True
                    break
                per_job -= 1
                budget -= 1
                wall = iterator.get_next(datetime)
                # Match the scheduler's wall-clock rule. Re-anchor after the spring gap.
                following = next((v for fold in (0, 1) if (v := wall.replace(tzinfo=zone, fold=fold).timestamp()) > stamp), None)
                if following is None:
                    raise ValueError("Non-advancing cron schedule")
                stamp = following
                actual_wall = datetime.fromtimestamp(stamp, zone).replace(tzinfo=None)
                if actual_wall != wall:
                    iterator = croniter(schedule["expr"], actual_wall, max_years_between_matches=8)
        except (ValueError, TypeError, OverflowError, KeyError, ImportError, OSError, AttributeError):
            incomplete = True
    return {"days": rows, "partial": incomplete}

