"""Replay every exchange of the fixture through the scene-tracking prompt and score it."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import fixture_campaign  # noqa: E402

from rpg_llm import router  # noqa: E402
from rpg_llm.config import Settings  # noqa: E402
from rpg_llm.llm import LLMClient  # noqa: E402
from rpg_llm.vault import Scene, State  # noqa: E402

# y = the exchange starts a new scene.
FIXTURES = {
    # 5: bar -> ship, 7: jump to Efate, 8: shuttle down to the brokers' hall, 13: back to the
    # ship. 6 is a blocked jump; 11/12 mention other places.
    "traveller": "nnnnynyynnnny",
    # One long stand-off in a bar and its back room (autoplayed). 12 has an NPC naming a
    # meeting place in dialogue.
    "autoplay": "nnnnnnnnnnnn",
}


async def run(name: str, labels: str, s: Settings, llm: LLMClient) -> int:
    c = fixture_campaign(name)
    msgs = c.messages()
    state = State(scenes=[Scene(1, 1)])
    ok = 0
    print(f"== {name}")
    for n, i in enumerate(range(0, len(msgs) - 1, 2)):
        t = time.time()
        v = await router._verdict(c, llm, state, msgs, i)
        if n == 0:
            v = {**v, "transition": False}  # as in track(): the opening exchange only sets location
        got = v["transition"] and v["confidence"] >= s.router_threshold
        exp = labels[n] == "y"
        ok += got == exp
        print(f"{'ok ' if got == exp else 'BAD'} ex{n + 1:<2} expect={'Y' if exp else 'n'} "
              f"got={'Y' if got else 'n'} {v['confidence']:.2f} {time.time() - t:4.1f}s "
              f"now={v.get('location_now')!r} | {v['reason'][:90]}")
        router._apply(state, v, msgs[i]["id"], s.router_threshold)
    return ok


async def main() -> None:
    s = Settings()
    llm = LLMClient(s.router)
    total = ok = 0
    for name, labels in FIXTURES.items():
        ok += await run(name, labels, s, llm)
        total += len(labels)
    print(f"{ok}/{total} correct")


asyncio.run(main())
