# db.py
from __future__ import annotations
import os, datetime as dt, warnings
from typing import Any, Optional, List, Dict
from psycopg_pool import AsyncConnectionPool

POOL: Optional[AsyncConnectionPool] = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS members (
    id          BIGSERIAL PRIMARY KEY,
    phone       TEXT UNIQUE NOT NULL,
    name        TEXT,
    email       TEXT,
    consent_ts  TIMESTAMPTZ,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS classes (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    coach       TEXT,
    capacity    INT NOT NULL DEFAULT 12
);

CREATE TABLE IF NOT EXISTS sessions (
    id          BIGSERIAL PRIMARY KEY,
    class_id    BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    start_time  TIME NOT NULL,
    end_time    TIME NOT NULL,
    UNIQUE (class_id, date, start_time)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'booking_status') THEN
        CREATE TYPE booking_status AS ENUM ('confirmed','waitlist','canceled');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS bookings (
    id          BIGSERIAL PRIMARY KEY,
    session_id  BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    member_id   BIGINT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    status      booking_status NOT NULL DEFAULT 'confirmed',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, member_id)
);
"""

SEED_SQL = """
INSERT INTO classes (title, coach, capacity)
SELECT * FROM (VALUES
  ('BodyPump','Alex',16),
  ('Yoga','Mara',12),
  ('Hyrox','Ken',10)
) AS v(title, coach, capacity)
WHERE NOT EXISTS (SELECT 1 FROM classes);

WITH base AS (
  SELECT id FROM classes
)
INSERT INTO sessions (class_id, date, start_time, end_time)
SELECT c.id, CURRENT_DATE, t.start_time, t.end_time
FROM base c
JOIN (VALUES
  (TIME '17:30', TIME '18:20'),
  (TIME '18:30', TIME '19:20'),
  (TIME '19:30', TIME '20:20')
) AS t(start_time, end_time) ON true
WHERE NOT EXISTS (SELECT 1 FROM sessions WHERE date = CURRENT_DATE);
"""

async def init_db(dsn: str):
    """Create pool, run schema + seed."""
    global POOL
    if not dsn:
        raise RuntimeError("DATABASE_URL missing")
    POOL = AsyncConnectionPool(dsn, max_size=5)
    await POOL.open()  # avoid deprecation warning
    async with POOL.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)
            await cur.execute(SEED_SQL)

async def close_db():
    global POOL
    if POOL:
        await POOL.close()
        POOL = None

async def ping_db() -> bool:
    if not POOL:
        return False
    try:
        async with POOL.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1;")
            await cur.fetchone()
        return True
    except Exception:
        return False

# ---------- helpers ----------
async def get_member_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    assert POOL
    async with POOL.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id, phone, name, email FROM members WHERE phone=%s;", (phone,))
        r = await cur.fetchone()
        if not r:
            return None
        return {"id": r[0], "phone": r[1], "name": r[2], "email": r[3]}

async def get_or_create_member_by_phone(phone: str, name: Optional[str]=None) -> Dict[str, Any]:
    assert POOL
    m = await get_member_by_phone(phone)
    if m:
        return m
    async with POOL.connection() as conn, conn.cursor() as cur:
        await cur.execute("INSERT INTO members (phone, name) VALUES (%s,%s) RETURNING id, phone, name, email;",
                          (phone, name))
        r = await cur.fetchone()
        return {"id": r[0], "phone": r[1], "name": r[2], "email": r[3]}

def _to_date_from_when(when: str | None) -> dt.date:
    today = dt.date.today()
    if not when:
        return today
    w = when.lower()
    if "morgen" in w or "tomorrow" in w:
        return today + dt.timedelta(days=1)
    if "heute" in w or "today" in w:
        return today
    try:
        return dt.date.fromisoformat(w)
    except Exception:
        return today

async def get_schedule(when: Optional[str]=None) -> List[Dict[str, Any]]:
    assert POOL
    the_date = _to_date_from_when(when)
    sql = """
    SELECT
      s.id, c.title, c.coach, c.capacity, s.date, s.start_time, s.end_time,
      c.capacity - COALESCE((
        SELECT count(*) FROM bookings b
        WHERE b.session_id = s.id AND b.status='confirmed'
      ),0) AS remaining
    FROM sessions s
    JOIN classes c ON c.id = s.class_id
    WHERE s.date = %s
    ORDER BY s.start_time;
    """
    async with POOL.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, (the_date,))
        rows = await cur.fetchall()
    return [{
        "session_id": r[0],
        "title": r[1],
        "coach": r[2],
        "capacity": r[3],
        "date": r[4].isoformat(),
        "start_time": r[5].strftime("%H:%M"),
        "end_time": r[6].strftime("%H:%M"),
        "remaining": r[7],
    } for r in rows]

async def book_class(session_id: int, member_id: int) -> Dict[str, Any]:
    assert POOL
    remain_sql = """
    SELECT
      s.id, c.title, c.coach, c.capacity, s.date, s.start_time, s.end_time,
      c.capacity - COALESCE((
        SELECT count(*) FROM bookings b
        WHERE b.session_id = s.id AND b.status='confirmed'
      ),0) AS remaining
    FROM sessions s
    JOIN classes c ON c.id = s.class_id
    WHERE s.id = %s
    """
    async with POOL.connection() as conn, conn.cursor() as cur:
        await cur.execute(remain_sql, (session_id,))
        r = await cur.fetchone()
        if not r:
            raise ValueError("Session not found")
        _, title, coach, capacity, date, start_time, end_time, remaining = r
        status = 'confirmed' if remaining > 0 else 'waitlist'
        await cur.execute(
            "INSERT INTO bookings (session_id, member_id, status) VALUES (%s,%s,%s) "
            "ON CONFLICT (session_id, member_id) DO UPDATE SET status=EXCLUDED.status "
            "RETURNING id;",
            (session_id, member_id, status),
        )
        bid = (await cur.fetchone())[0]
        return {
            "booking_id": bid,
            "status": status,
            "title": title,
            "date": date.isoformat(),
            "start_time": start_time.strftime("%H:%M"),
            "end_time": end_time.strftime("%H:%M"),
            "coach": coach,
            "session_id": session_id,
            "member_id": member_id,
        }

async def get_booking_full(booking_id: int) -> Optional[Dict[str, Any]]:
    """Join booking + session + class + member for ICS/export."""
    assert POOL
    sql = """
    SELECT
      b.id, b.status, m.id, m.name, m.phone, m.email,
      s.id, s.date, s.start_time, s.end_time,
      c.id, c.title, c.coach
    FROM bookings b
    JOIN members m ON m.id = b.member_id
    JOIN sessions s ON s.id = b.session_id
    JOIN classes c  ON c.id = s.class_id
    WHERE b.id = %s
    """
    async with POOL.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, (booking_id,))
        r = await cur.fetchone()
        if not r:
            return None
        return {
            "booking_id": r[0],
            "status": r[1],
            "member": {"id": r[2], "name": r[3], "phone": r[4], "email": r[5]},
            "session": {
                "id": r[6],
                "date": r[7],
                "start_time": r[8],
                "end_time": r[9],
            },
            "class": {"id": r[10], "title": r[11], "coach": r[12]},
        }
