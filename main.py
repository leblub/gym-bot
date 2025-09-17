# main.py
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import httpx

# .env laden (nur lokal, auf Render kommen die Werte über Environment)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

app = FastAPI()

# Env Vars
VERIFY = os.getenv("META_VERIFY_TOKEN", "supersecretverify")
META_TOKEN = os.getenv("META_TOKEN", "REPLACE_ME")
PHONE_ID = os.getenv("PHONE_NUMBER_ID", "1234567890")
WA_BASE = f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages"

# Health-Route (zum schnellen Check im Browser)
@app.get("/")
async def health():
    return {"ok": True}

# Webhook-Verifizierung (GET)
@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY and challenge:
        # Muss exakt den Challenge als Plaintext zurückgeben
        return PlainTextResponse(challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")

# Webhook-Eingang (POST) → Echo-Antwort
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

    # Antwort zurückschicken
    payload = {
        "messaging_product": "whatsapp",
        "to": from_number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": f"✅ Bot live – du schriebst: {text}"
        }
    }
    headers = {"Authorization": f"Bearer {META_TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(WA_BASE, headers=headers, json=payload)
        r.raise_for_status()
    return {"ok": True}
