# ics.py
import datetime as dt
import uuid

def _fmt_dt(date: dt.date, time: dt.time) -> str:
    # naive local → treat as UTC-less simple form; for production: tz handling.
    return dt.datetime.combine(date, time).strftime("%Y%m%dT%H%M%S")

def make_booking_ics(b: dict) -> str:
    """
    b = get_booking_full(...) Ergebnis
    """
    uid = f"{b['booking_id']}@gym-bot"
    cls = b["class"]["title"]
    coach = b["class"]["coach"] or ""
    date = b["session"]["date"]
    start = b["session"]["start_time"]
    end = b["session"]["end_time"]

    dtstart = _fmt_dt(date, start)
    dtend   = _fmt_dt(date, end)

    summary = f"{cls} ({coach})" if coach else cls
    desc = f"Buchung #{b['booking_id']} – Status: {b['status']}"

    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//gym-bot//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")}
DTSTART:{dtstart}
DTEND:{dtend}
SUMMARY:{summary}
DESCRIPTION:{desc}
LOCATION:Fitnessstudio
BEGIN:VALARM
TRIGGER:-PT2H
ACTION:DISPLAY
DESCRIPTION:Erinnerung: {summary}
END:VALARM
END:VEVENT
END:VCALENDAR
""".strip()
    return ics
