# db.py
from __future__ import annotations
import os, datetime as dt
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

-- einzelne Termin-Instanzen (Kurstermine)
CREATE TABLE IF NOT EXISTS sessions (
    id          BIGSERIAL PRIMARY KEY,
    class_id    BIGINT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    start_time  TIME NOT NULL,
    end_time    TIME NOT NULL,
    UNIQUE (class_id, date, start_time)
);

CREATE TYPE booking_status AS ENUM ('confirmed','waitlist','canceled');
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
-- Demo-Daten (nur wenn leer)
INSERT INTO classes (title, coach, capacity)
SELECT * FROM (VALUES
  ('BodyPump','Alex',16),
  ('Yoga','Mara',12),
  ('Hyrox','Ken',10)
) AS v(title, coach, capacity)
WHERE NOT EXISTS (SELECT 1 FROM classes);

-- Erzeuge Termine für heute, wenn keine da sind
WITH base AS (
  SELECT id,title,capacity FROM classes
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
    global POOL
    if not dsn:
        raise RuntimeError("DATABASE_URL missing")
    # Async Connection Pool
    POOL = AsyncConnectionPool(dsn, max_size=5)
    async with POOL.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)
            await cur.execute(SEED_SQL)

async def ping_db() -> bool:
    if not POOL:
        return False
    try:
        async with POOL.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1;")
                await cur.fetchone()
        return True
    except Exception:
        return False

# ---------- helpers ----------
async def get_or_create_member_by_phone(phone: str, name: Optional[str]=None) -> Dict[str, Any]:
    assert POOL
    async with POOL.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id, phone, name FROM members WHERE phone=%s;", (phone,))
        row = await cur.fetchone()
        if row:
            return {"id": row[0], "phone": row[1], "name": row[2]}
        await cur.execute("INSERT INTO members (phone, name) VALUES (%s,%s) RETURNING id, phone, name;",
                          (phone, name))
        r = await cur.fetchone()
        return {"id": r[0], "phone": r[1], "name": r[2]}

def _to_date_from_when(when: str | None) -> dt.date:
    today = dt.date.today()
    if not when:
        return today
    w = when.lower()
    if "morgen" in w or "tomorrow" in w:
        return today + dt.timedelta(days=1)
    if "heute" in w or "today" in w:
        return today
    # yyyy-mm-dd
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
    out = []
    for r in rows:
        out.append({
            "session_id": r[0],
            "title": r[1],
            "coach": r[2],
            "capacity": r[3],
            "date": r[4].isoformat(),
            "start_time": r[5].strftime("%H:%M"),
            "end_time": r[6].strftime("%H:%M"),
            "remaining": r[7],
        })
    return out

async def book_class(session_id: int, member_id: int) -> Dict[str, Any]:
    assert POOL
    # check remaining
    remain_sql = """
    SELECT
      c.title, c.coach, c.capacity, s.date, s.start_time, s.end_time,
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
        title, coach, capacity, date, start_time, end_time, remaining = r
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
        }
