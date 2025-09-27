# main.py
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, Response
import httpx
import redis.asyncio as redis

from agent import run_agent, Memory
from db import init_db, close_db, ping_db, get_booking_full
from ics import make_booking_ics

app = FastAPI()

# ENV
VERIFY = os.getenv("META_VERIFY_TOKEN", "supersecretverify")
META_TOKEN = os.getenv("META_TOKEN", "REPLACE_ME")
PHONE_ID = os.getenv("PHONE_NUMBER_ID", "1234567890")
WA_BASE = f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages"
REDIS_URL = os.getenv("REDIS_URL", "")
r = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
memory = Memory(r) if r else None

# Startup / Shutdown
@app.on_event("startup")
async def _startup():
    dsn = os.getenv("DATABASE_URL", "")
    if dsn:
        try:
            await init_db(dsn)
            print("DB initialized ✔")
        except Exception as e:
            print("DB init failed:", repr(e))
    else:
        print("DATABASE_URL missing, DB disabled")

@app.on_event("shutdown")
async def _shutdown():
    await close_db()

# Helpers
async def send_text(to: str, body: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": True, "body": body}
    }
    headers = {"Authorization": f"Bearer {META_TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(WA_BASE, headers=headers, json=payload)
        if resp.status_code >= 400:
            print("WA send error:", resp.status_code, resp.text)

# Health
@app.get("/")
async def health():
    redis_ok = False
    if r:
        try:
            redis_ok = bool(await r.ping())
        except Exception as e:
            print("Redis ping failed:", repr(e))
    db_ok = await ping_db() if os.getenv("DATABASE_URL") else False
    return {"ok": True, "details": {"redis": redis_ok, "db": db_ok}}

# Webhook Verify
@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY
        and params.get("hub.challenge")
    ):
        return PlainTextResponse(params.get("hub.challenge"), status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")

# Incoming
@app.post("/webhook")
async def incoming(req: Request):
    body = await req.json()
    try:
        entry = body["entry"][0]["changes"][0]["value"]
        msgs = entry.get("messages", [])
        if not msgs:
            return {"ok": True}
        msg = msgs[0]
        from_number = msg["from"]
        text = (msg.get("text") or {}).get("body", "").strip()
    except Exception:
        return {"ok": True}

    if text.lower() in {"stop", "abbrechen", "cancel", "ende"}:
        if memory:
            try:
                await r.delete(f"history:{from_number}")
            except Exception as e:
                print("Redis delete failed:", repr(e))
        await send_text(from_number, "Okay, der Dialog ist beendet. Wie kann ich sonst helfen?")
        return {"ok": True}

    if not memory:
        await send_text(from_number, "Interner Fehler: Memory nicht konfiguriert.")
        return {"ok": False}

    try:
        reply = await run_agent(from_number, text, memory)
    except Exception as e:
        print("Agent error:", repr(e))
        reply = "Uff, hier ist etwas schiefgelaufen. Kannst du es noch einmal versuchen?"

    await send_text(from_number, reply)
    return {"ok": True}

# Test-Agent (ohne WhatsApp)
@app.post("/test-agent")
async def test_agent(req: Request):
    data = await req.json()
    user_id = data.get("user_id", "demo")
    text = data.get("text", "")
    if not memory:
        return {"ok": False, "error": "Memory not configured"}
    reply = await run_agent(user_id, text, memory)
    return {"ok": True, "reply": reply}

# ICS Download (z. B. https://.../ics/123.ics)
@app.get("/ics/{booking_id}.ics")
async def dl_ics(booking_id: int):
    b = await get_booking_full(booking_id)
    if not b:
        raise HTTPException(404, "Booking not found")
    ics = make_booking_ics(b)
    headers = {
        "Content-Disposition": f'attachment; filename="booking-{booking_id}.ics"'
    }
    return Response(content=ics, media_type="text/calendar; charset=utf-8", headers=headers)
