# main.py
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import httpx
import redis.asyncio as redis

# .env laden (lokal)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from agent import run_agent, Memory  # Agent + Memory

app = FastAPI()

# ====== ENV ======
VERIFY = os.getenv("META_VERIFY_TOKEN", "supersecretverify")
META_TOKEN = os.getenv("META_TOKEN", "REPLACE_ME")
PHONE_ID = os.getenv("PHONE_NUMBER_ID", "1234567890")
WA_BASE = f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages"

# IMPORTANT: keine ssl-Argumente übergeben; Schema bestimmt TLS
REDIS_URL = os.getenv("REDIS_URL", "")
r = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
memory = Memory(r) if r else None

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
    redis_ok = False
    if r:
        try:
            redis_ok = bool(await r.ping())
        except Exception as e:
            print("Redis ping failed:", repr(e))
    return {"ok": True, "details": {"redis": redis_ok}}

# ====== Webhook Verify ======
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
        if memory:
            try:
                await r.delete(f"history:{from_number}")
            except Exception as e:
                print("Redis delete failed:", repr(e))
        await send_text(from_number, "Okay, der Dialog ist beendet. Wie kann ich sonst helfen?")
        return {"ok": True}

    # Agent mit Memory/Tools
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

# ====== Test-Agent (ohne WhatsApp) ======
@app.post("/test-agent")
async def test_agent(req: Request):
    data = await req.json()
    user_id = data.get("user_id", "demo")
    text = data.get("text", "")
    if not memory:
        return {"ok": False, "error": "Memory not configured"}
    reply = await run_agent(user_id, text, memory)
    return {"ok": True, "reply": reply}
