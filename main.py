# main.py
import os
from fastapi import FastAPI, Request, HTTPException
import httpx

# .env laden (falls vorhanden)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

app = FastAPI()

VERIFY = os.getenv("META_VERIFY_TOKEN", "supersecretverify")
META_TOKEN = os.getenv("META_TOKEN", "REPLACE_ME")
PHONE_ID = os.getenv("PHONE_NUMBER_ID", "1234567890")
WA_BASE = f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages"

@app.get("/webhook")
async def verify(hub_mode: str | None = None,
                 hub_challenge: str | None = None,
                 hub_verify_token: str | None = None):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY:
        return int(hub_challenge or "0")
    raise HTTPException(403, "Verification failed")

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
        text = (msg.get("text") or {}).get("body", "")
    except Exception:
        return {"ok": True}

    # Echo-Antwort
    payload = {
        "messaging_product": "whatsapp",
        "to": from_number,
        "type": "text",
        "text": {"preview_url": False, "body": f"✅ Bot live – du schriebst: {text}"}
    }
    headers = {"Authorization": f"Bearer {META_TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(WA_BASE, headers=headers, json=payload)
        r.raise_for_status()
    return {"ok": True}
