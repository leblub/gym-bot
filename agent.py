# agent.py
import os, json, asyncio
from openai import AsyncOpenAI

# OpenAI Setup
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Memory-Wrapper für Redis
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
        await self.r.ltrim(key, -20, -1)  # max 20 Nachrichten behalten

    async def clear(self, user_id: str):
        await self.r.delete(f"history:{user_id}")

# Beispiel-Tools
async def tool_kursplan():
    return "Heute: Yoga 18:30, Spinning 19:00, Crossfit 20:00"

async def tool_buchen(kurs, uhrzeit):
    return f"✅ Dein Platz für {kurs} um {uhrzeit} wurde reserviert."

TOOLS = {
    "kursplan": tool_kursplan,
    "buchen": tool_buchen,
}

# Agent-Logik
async def run_agent(user_id: str, user_text: str, memory: Memory) -> str:
    # Schritt 1: Verlauf laden
    history = await memory.get_history(user_id)

    # Schritt 2: User-Input speichern
    await memory.add_message(user_id, "user", user_text)

    # Schritt 3: Prompt bauen
    messages = [
        {"role": "system", "content": "Du bist ein hilfreicher Gym-Assistent. "
         "Du kannst Kurspläne zeigen und Buchungen simulieren. Sprich locker und freundlich."}
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    # Schritt 4: OpenAI anfragen
    resp = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.6,
    )

    reply = resp.choices[0].message.content.strip()

    # Schritt 5: Antwort speichern
    await memory.add_message(user_id, "assistant", reply)

    return reply
