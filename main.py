# main.py
import os, json, re
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import httpx
import redis.asyncio as redis

# .env für lokal
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from agent import run_agent, Memory  # <— unser Agent

app = FastAPI()

# ====== ENV ======
VERIFY = os.getenv("META_VERIFY_TOKEN", "supersecretverify")
META_TOKEN = os.getenv("META_TOKEN", "REPLACE_ME")
PHONE_ID = os.getenv("PHONE_NUMBER_ID", "1234567890")
WA_BASE = f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages"

REDIS_URL = os.getenv("REDIS_URL")
REDIS_TLS = os.getenv("REDIS_TLS", "true").lower() == "true"
redis_kwargs = {}
if REDIS_URL:
    redis_kwargs = {"ssl": True} if REDIS_TLS else {}
r = redis.from_url(REDIS_URL, decode_responses=True, **redis_kwargs) if REDIS_URL else None
memory = Memory(r) if r else None  # ohne Redis würde Agent nicht laufen → setze REDIS_URL!

# ====== Helpers ======
async def send_text(to: str, body: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body}
    }
    headers = {"Authorization": f"Bearer {META_TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(WA_BASE, headers=headers, json=payload)
        if resp.status_code >= 400:
            print("WA send error:", resp.status_code, resp.text)

# ====== Health ======
@app.get("/")
async def health():
    ok = True
    details = {"redis": bool(r)}
    return {"ok": ok, "details": details}

# ====== Webhook Verify ======
@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY and params.get("hub.challenge"):
        return PlainTextResponse(params.get("hub.challenge"), status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")

# ====== Incoming ======
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

    # einfache Systemkommandos
    if text.lower() in {"stop", "abbrechen", "cancel", "ende"}:
        # kurz den Verlauf löschen
        if memory:
            await r.delete(f"history:{from_number}")
        await send_text(from_number, "Okay, der Dialog ist beendet. Wie kann ich sonst helfen?")
        return {"ok": True}

    # Agent ausführen (mit Memory/Tools)
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
