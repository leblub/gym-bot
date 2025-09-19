# main.py
import os
import re
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import httpx

# .env lokal laden (auf Render kommen die Werte über Environment)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

app = FastAPI()

# ==== Environment ====
VERIFY = os.getenv("META_VERIFY_TOKEN", "supersecretverify")
META_TOKEN = os.getenv("META_TOKEN", "REPLACE_ME")
PHONE_ID = os.getenv("PHONE_NUMBER_ID", "1234567890")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")            # optional
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini") # optional

WA_BASE = f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages"

# ==== Health ====
@app.get("/")
async def health():
    return {"ok": True}

# ==== Webhook-Verify (GET) ====
@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY and challenge:
        # Wichtig: Challenge als Plain-Text zurückgeben
        return PlainTextResponse(challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")

# ==== Intent-Klassifikation ====
def classify_intent_rule_based(text: str) -> dict:
    t = text.lower().strip()
    if any(k in t for k in ["probe", "probetraining", "trial", "test"]):
        return {"intent": "lead.probetraining", "entities": {}}
    if any(k in t for k in ["kursplan", "kurse", "heute", "plan", "schedule"]):
        ent = {"when": "today" if "heute" in t else None}
        return {"intent": "class.plan", "entities": ent}
    if any(k in t for k in ["buch", "book", "reservier"]):
        tm = re.search(r"\b(\d{1,2}[:.]\d{2})\b", t)
        course = None
        for k in ["yoga", "bodypump", "hyrox", "hiit", "spinning"]:
            if k in t:
                course = k
        return {"intent": "class.book", "entities": {"time": tm.group(1) if tm else None, "course": course}}
    if any(k in t for k in ["öffnungs", "zeiten", "open", "hours"]):
        return {"intent": "faq.hours", "entities": {}}
    if "hilfe" in t or "mitarbeiter" in t:
        return {"intent": "fallback.handover", "entities": {}}
    return {"intent": "fallback.unknown", "entities": {}}

async def classify_intent(text: str) -> dict:
    # Ohne OpenAI-Key → Regeln
    if not OPENAI_API_KEY:
        return classify_intent_rule_based(text)

    # Mit OpenAI → robustere Erkennung
    prompt = (
        "Du bist ein Intent-Classifier für ein Fitnessstudio. "
        "Gib NUR JSON zurück: {\"intent\":\"...\",\"entities\":{...}}. "
        "Erlaubte intents: lead.probetraining, class.plan, class.book, class.cancel, "
        "pt.book, membership.pause, membership.cancel, faq.hours, fallback.handover. "
        "Entities z.B. time, date, course, trainer. Wenn unklar: "
        "{\"intent\":\"fallback.unknown\",\"entities\":{}}."
    )
    data = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text}
        ],
        "temperature": 0
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:
        return classify_intent_rule_based(text)

# ==== Webhook (POST) – Nachricht empfangen & antworten ====
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
        # Nichts Relevantes enthalten
        return {"ok": True}

    # Intent erkennen
    intent_data = await classify_intent(text)
    intent = intent_data.get("intent", "fallback.unknown")
    ent = intent_data.get("entities", {})

    # Antwort-Templates (MVP)
    if intent == "lead.probetraining":
        reply = ("Top! Für ein Probetraining brauche ich kurz deinen Vornamen "
                 "und eine Wunschzeit (z. B. morgen 18:30).")
    elif intent == "class.plan":
        reply = ("Kursplan heute:\n"
                 "• 17:30 BodyPump (3 Plätze)\n"
                 "• 18:30 Yoga (2 Plätze)\n"
                 "• 19:30 Hyrox (Warteliste)\n"
                 "Wenn du magst, schreib: 'buch 18:30 Yoga'.")
    elif intent == "class.book":
        when = ent.get("time") or "deine Wunschzeit"
        course = ent.get("course") or "deinen Kurs"
        reply = f"Alles klar – ich reserviere {course} um {when}. Bestätigst du mit 'ja'?"
    elif intent == "faq.hours":
        reply = "Unsere Öffnungszeiten: Mo–Fr 06–22 Uhr, Sa–So 08–20 Uhr."
    elif intent == "fallback.handover":
        reply = "Ich verbinde dich mit dem Team. Bitte einen Moment 🙏"
    else:
        reply = ("Ich helfe dir bei Probetraining, Kurs buchen, Öffnungszeiten. "
                 "Schreib z. B.: 'Probetraining morgen 18:30'.")

    # WhatsApp Antwort senden
    payload = {
        "messaging_product": "whatsapp",
        "to": from_number,
        "type": "text",
        "text": {"preview_url": False, "body": reply}
    }
    headers = {"Authorization": f"Bearer {META_TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(WA_BASE, headers=headers, json=payload)
        if resp.status_code >= 400:
            # Details ins Log schreiben, aber trotzdem 200 an Meta (sonst retried Meta die Zustellung)
            print("WA send error:", resp.status_code, resp.text)
            return {"ok": False, "wa_status": resp.status_code}

    return {"ok": True}
