"""Check which wiki notes the gatekeeper pulls in for a handful of player messages."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import fixture_campaign  # noqa: E402

from rpg_llm import router  # noqa: E402
from rpg_llm.config import Settings  # noqa: E402
from rpg_llm.llm import LLMClient  # noqa: E402

# (message, substrings every expected note path must contain; empty = nothing should be added)
CASES = [
    ("I take the Kelvin-Orr ore job and ask when the ore will be ready to load.", set()),
    ("I check the fuel gauge and ask Dex how the drive is holding up.", set()),
    ("Has a skinny man in a grey coat been through here lately?", {"pell"}),
    ("I wonder aloud if that loan shark back home has put a bounty on us yet.", {"brandt"}),
    ("Before we leave, I want to go back to the bar where we met the broker.", {"bar"}),
    ("I haggle over the price of fuel.", set()),
]


async def main() -> None:
    s = Settings()
    c = fixture_campaign("traveller")
    llm = LLMClient(s.router)
    last = c.messages()[-1]["content"]
    ok = 0
    for msg, want in CASES:
        t = time.time()
        _, info = await router.gatekeep(c, llm, msg, last, 60)
        got = info["injected"]
        good = all(any(w in p for p in got) for w in want) and \
            all(any(w in p for w in want) for p in got)
        ok += good
        print(f"{'ok ' if good else 'BAD'} {time.time() - t:4.1f}s want={sorted(want)} got={got}")
    print(f"{ok}/{len(CASES)} correct")


asyncio.run(main())
