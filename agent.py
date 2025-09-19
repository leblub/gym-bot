# agent.py
import os, json, time, re
import httpx
from typing import Any, Dict

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# --------- Tools (hier hängen später echte DB-Aufrufe dran) ----------
async def tool_get_schedule(args: Dict[str, Any]) -> Dict[str, Any]:
    when = (args.get("when") or "today").lower()
    # TODO: später aus Postgres laden
    demo = [
        {"time":"17:30", "title":"BodyPump", "remaining":3},
        {"time":"18:30", "title":"Yoga", "remaining":2},
        {"time":"19:30", "title":"Hyrox", "remaining":0, "waitlist":True},
    ]
    return {"when": when, "classes": demo}

async def tool_book_class(args: Dict[str, Any]) -> Dict[str, Any]:
    # TODO: später echte Buchung (DB + Kapazität)
    course = args.get("course") or "Yoga"
    time_ = args.get("time") or "18:30"
    user_id = args.get("user_id")
    return {"ok": True, "course": course, "time": time_, "booking_id": "demo-" + str(int(time.time()))}

async def tool_handover(args: Dict[str, Any]) -> Dict[str, Any]:
    # TODO: z. B. Ticket in n8n / Slack
    note = args.get("note") or ""
    return {"ok": True, "note": note, "routed_to": "human"}

TOOLS = {
    "get_schedule": {"fn": tool_get_schedule, "desc": "Kursplan abrufen", "params": {"when":"string"}},
    "book_class":   {"fn": tool_book_class,   "desc": "Kurs buchen",      "params": {"course":"string","time":"string","user_id":"string"}},
    "handover":     {"fn": tool_handover,     "desc": "an Mensch übergeben","params":{"note":"string"}},
}

def tools_schema_for_openai():
    # OpenAI "tools" (function calling) Schema
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": meta["desc"],
                "parameters": {
                    "type": "object",
                    "properties": {k: {"type":"string"} for k in meta["params"].keys()},
                    "required": [k for k in meta["params"].keys() if k in ("when","course","time")]
                }
            }
        } for name, meta in TOOLS.items()
    ]

# --------- Memory layer (kurzer Verlauf + Profil) ----------
class Memory:
    def __init__(self, redis_client, ttl_sec=60*60):
        self.r = redis_client
        self.ttl = ttl_sec

    async def get_profile(self, user_id: str) -> Dict[str, Any]:
        raw = await self.r.get(f"profile:{user_id}")
        return json.loads(raw) if raw else {}

    async def set_profile(self, user_id: str, data: Dict[str, Any]):
        await self.r.set(f"profile:{user_id}", json.dumps(data), ex=self.ttl)

    async def get_history(self, user_id: str) -> list:
        raw = await self.r.get(f"history:{user_id}")
        return json.loads(raw) if raw else []

    async def append_history(self, user_id: str, role: str, content: str, keep_last=10):
        hist = await self.get_history(user_id)
        hist.append({"role": role, "content": content, "ts": int(time.time())})
        hist = hist[-keep_last:]
        await self.r.set(f"history:{user_id}", json.dumps(hist), ex=self.ttl)

# --------- Core Agent ----------
SYSTEM_PROMPT = (
    "Du bist der Studio-Assistent eines Fitnessstudios. "
    "Ziele: Hilf beim Probetraining, Kursplan, Buchungen, Öffnungszeiten. "
    "Wenn eine Aktion nötig ist, rufe ein passendes TOOL auf. "
    "Sei kurz und konkret. Falls unklar, frag nach. "
    "Wenn menschliche Hilfe nötig: nutze das 'handover'-Tool.\n\n"
    "Gib NUR die End-Antwort an den Nutzer zurück, außer du musst ein Tool aufrufen."
)

async def run_agent(user_id: str, user_text: str, memory: Memory) -> str:
    """
    Gibt eine Text-Antwort zurück. Ruft Tools ggf. mehrfach auf (ReAct-Schleife light).
    """
    await memory.append_history(user_id, "user", user_text)
    history = await memory.get_history(user_id)
    profile = await memory.get_profile(user_id)

    msgs = [{"role":"system","content": SYSTEM_PROMPT}]
    # Minimaler Kontext
    if profile:
        msgs.append({"role":"system","content": f"PROFILE: {json.dumps(profile, ensure_ascii=False)}"})
    # kurze History
    for m in history[-6:]:
        msgs.append({"role":"user" if m["role"]=="user" else "assistant", "content": m["content"]})

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    tools = tools_schema_for_openai()

    for _ in range(3):  # max 3 Tool-Schritte
        data = {"model": OPENAI_MODEL, "messages": msgs, "tools": tools, "tool_choice": "auto", "temperature": 0}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data)
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]

        # ruft das Modell ein Tool?
        if "tool_calls" in out and out["tool_calls"]:
            tc = out["tool_calls"][0]
            tool_name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
            fn = TOOLS.get(tool_name, {}).get("fn")
            tool_result = {}
            if fn:
                tool_result = await fn(args)
            msgs.append(out)  # assistant message mit tool_call
            msgs.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": tool_name,
                "content": json.dumps(tool_result, ensure_ascii=False)
            })
            continue  # nochmal zum Modell, jetzt mit Tool-Ergebnis

        # keine Tools → finale Antwort
        final = out.get("content") or ""
        await memory.append_history(user_id, "assistant", final)
        return final

    # Falls Schleife erschöpft
    fallback = "Ich habe das nicht eindeutig verstanden. Möchtest du Kursplan sehen oder ein Probetraining buchen?"
    await memory.append_history(user_id, "assistant", fallback)
    return fallback
