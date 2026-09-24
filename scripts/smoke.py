"""Check the configured endpoints: context window, a chat reply, and a tool call.

    uv run python scripts/smoke.py
"""

import asyncio
import time

from rpg_llm.config import Settings
from rpg_llm.llm import LLMClient

LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look up a location or NPC in the campaign wiki by name",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"]},
    },
}


async def check(name: str, llm: LLMClient) -> None:
    print(f"== {name}: {llm.slot.model} @ {llm.slot.base_url}")
    print("   context window:", await llm.context_window())

    t = time.time()
    msg = await llm.chat([{"role": "user", "content": "Reply with exactly: ready"}], max_tokens=400)
    print(f"   chat: {msg.get('content')!r} ({time.time() - t:.1f}s,"
          f" reasoning {'yes' if msg.get('reasoning_content') else 'no'})")

    msg = await llm.chat(
        [{"role": "system", "content": "You are a game master. Use tools to look up wiki notes."},
         {"role": "user", "content": "What do we know about the broker Tavi Orsk?"}],
        tools=[LOOKUP_TOOL], max_tokens=1500,
    )
    calls = msg.get("tool_calls") or []
    print("   tool call:", [(c["function"]["name"], c["function"]["arguments"]) for c in calls]
          or "NONE")


async def main() -> None:
    s = Settings.load()
    seen = set()
    for name, slot in [("dm", s.dm), ("router", s.router), ("archiver", s.archiver)]:
        if slot in seen:
            print(f"== {name}: same as above")
            continue
        seen.add(slot)
        await check(name, LLMClient(slot))


asyncio.run(main())
