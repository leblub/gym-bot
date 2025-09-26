# agent.py
import os, json
from typing import Any, Dict
from openai import AsyncOpenAI

from db import get_schedule, get_or_create_member_by_phone, book_class

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

class Memory:
    def __init__(self, redis_client):
        self.r = redis_client

    async def get_history(self, user_id: str):
        key = f"history:{user_id}"
        msgs = await self.r.lrange(key, 0, -1)
        return [json.loads(m) for m in msgs]

    async def add_message(self, user_id: str, role: str, content: str):
        key = f"history:{user_id}"
        entry = {"role": role, "content": content}
        await self.r.rpush(key, json.dumps(entry))
        await self.r.ltrim(key, -20, -1)

    async def clear(self, user_id: str):
        await self.r.delete(f"history:{user_id}")

SYSTEM = (
    "Du bist der Assistent eines Fitnessstudios. "
    "Du kannst Kurspläne anzeigen und Buchungen vornehmen. "
    "Wenn der User seine Telefonnummer nutzt, ist das seine ID. "
    "Antworten kurz, freundlich, in Deutsch. Wenn nötig: Rückfragen stellen."
)

async def run_agent(user_id: str, user_text: str, memory: Memory) -> str:
    # Verlauf speichern
    hist = await memory.get_history(user_id)
    await memory.add_message(user_id, "user", user_text)

    # einfache Heuristik: 'plan'/'heute' -> get_schedule; 'buch' inkl. ID -> book
    t = user_text.lower()
    if any(k in t for k in ["plan", "heute", "kursplan", "schedule"]):
        when = "heute" if "heute" in t else ("morgen" if "morge" in t or "morgen" in t else None)
        rows = await get_schedule(when)
        if not rows:
            reply = "Heute stehen keine Kurse im Plan. Versuch es mit einem anderen Tag."
        else:
            lines = []
            for r in rows:
                lines.append(f"#{r['session_id']} {r['start_time']} {r['title']} ({r['remaining']} frei)")
            reply = "Kursplan:\n" + "\n".join(lines) + "\n\nBuchen: z.B. 'buch #ID'."
        await memory.add_message(user_id, "assistant", reply)
        return reply

    # buchungsversuch: "buch #123" oder "buch 123"
    import re
    m = re.search(r"buch\s*#?\s*(\d+)", t)
    if m:
        session_id = int(m.group(1))
        member = await get_or_create_member_by_phone(user_id)
        res = await book_class(session_id, member["id"])
        if res["status"] == "confirmed":
            reply = (f"✅ Eingetragen: {res['title']} am {res['date']} um {res['start_time']}. "
                     f"(Buchungs-ID {res['booking_id']})")
        else:
            reply = (f"ℹ️ Warteliste: {res['title']} am {res['date']} um {res['start_time']}. "
                     f"Ich sag dir Bescheid, wenn ein Platz frei wird.")
        await memory.add_message(user_id, "assistant", reply)
        return reply

    # ansonsten LLM-Antwort (Small Talk / FAQ)
    msgs = [{"role":"system","content": SYSTEM}]
    msgs.extend(hist[-6:])
    msgs.append({"role":"user","content": user_text})

    resp = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=msgs,
        temperature=0.6,
    )
    reply = resp.choices[0].message.content.strip()
    await memory.add_message(user_id, "assistant", reply)
    return reply
