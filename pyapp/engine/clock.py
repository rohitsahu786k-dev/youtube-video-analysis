"""Time-zone aware helpers: window tests, session labels and IST/UTC rendering (DST-correct via zoneinfo)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pyapp.engine.series import parse_hhmm
from pyapp.engine.spec import Window

# Conventional local trading hours used ONLY to label journal rows. They are not instructor rules.
LABEL_SESSIONS = (("Asia", "Asia/Tokyo", 9 * 60, 18 * 60), ("London", "Europe/London", 8 * 60, 17 * 60), ("New York", "America/New_York", 8 * 60, 17 * 60))


def local(ts, tz):
    return datetime.fromtimestamp(ts, ZoneInfo(tz))


def fmt(ts, tz):
    return local(ts, tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def iso_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_window(window: Window, ts):
    when = local(ts, window.tz)
    minute = when.hour * 60 + when.minute
    start, end = parse_hhmm(window.start), parse_hhmm(window.end)
    if start < end:
        inside = start <= minute < end
        weekday = when.weekday()
    else:
        inside = minute >= start or minute < end
        weekday = when.weekday() if minute >= start else (when.weekday() - 1) % 7
    return inside and weekday in window.days


def session_label(ts):
    names = []
    for name, tz, start, end in LABEL_SESSIONS:
        when = local(ts, tz)
        if when.weekday() < 5 and start <= when.hour * 60 + when.minute < end:
            names.append(name)
    if not names:
        return "Off-hours"
    return "/".join(names) if len(names) > 1 else names[0]
