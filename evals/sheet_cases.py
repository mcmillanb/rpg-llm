"""Hand-written sheet-update cases: things promised, quoted or offered must not land on the sheet
as if they had happened; things that did happen must.

    uv run python evals/sheet_cases.py [--runs 5]

Each case runs --runs times against the router model; prints a pass count per case.
"""

import argparse
import asyncio
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rpg_llm import router, sheet  # noqa: E402
from rpg_llm.config import Settings  # noqa: E402
from rpg_llm.llm import LLMClient  # noqa: E402
from rpg_llm.vault import Vault  # noqa: E402

START = {"name": "Callen Vane", "concept": "Disabled Navy veteran, intuitive pilot",
         "money": "204 Cr", "gear": ["Work Gloves", "Basic Hand Tools", "Thermal Reseal Compound"],
         "assets": ["Military utility truck", "Starship Aethelgard (buried)"],
         "obligations": ["Complete atmospheric test flight within three months"]}

ORDERED = ("Message Kess for the filters, if he can get them I will pick them up tomorrow, in the "
           "meantime go to the trading post in Oren and see if there is a contract I can do today",
           "You send a quick text to B. Kess: *Need Mk-IV military life support filters by tomorrow "
           "morning.* His reply comes back almost instantly. *Dock 12, first light. Seventy credits "
           "for the filters, twenty for my trouble.*\n\nIt burns a good chunk of your recent "
           "earnings, but clean air in orbit is worth the price. You climb out of the crawlspace and "
           "make the drive into Oren.\n\nThe clerk behind the high glass counter looks up as you walk "
           "in. \"New delivery or looking for another haul?\" she asks. \"We just got in a request for "
           "the municipal water plant; they need three crates of heavy filtration media hauled down to "
           "the reservoir by evening. Twenty-five credits cash on hand.\"")
BOUGHT = ("I pay Kess and take the filters.",
          "You count out ninety credits onto the desk. Kess sweeps them into a drawer and slides a "
          "sealed crate across: two Mk-IV military life support filters, still in their wrap.")
ACCEPTED = ("I'll take the reservoir job.",
            "The clerk stamps the contract and pushes it under the glass. \"Three crates of "
            "filtration media to the reservoir by evening. Twenty-five on delivery.\" A dock hand "
            "loads the crates into your truck bed.")


def money(s):
    m = re.search(r"\d[\d,]*", s["money"])
    return int(m.group().replace(",", "")) if m else None


def has(items, *words):
    return any(w in i.lower() for i in items for w in words)


CASES = [
    # (the player went looking for a contract, so listing the offered job is a fair reading)
    ("ordered and quoted: no payment, no filters yet, pickup noted", ORDERED,
     lambda s: money(s) == 204 and not has(s["gear"], "filter") and has(s["obligations"], "kess")),
    ("bought and paid: money down, filters in gear", BOUGHT,
     lambda s: money(s) == 114 and has(s["gear"], "filter")),
    ("job accepted: obligation, no money yet", ACCEPTED,
     lambda s: money(s) == 204 and has(s["obligations"], "reservoir", "filtration media", "crates")),
]


async def run_case(llm, exchange) -> dict:
    tmp = Path(tempfile.mkdtemp())
    c = Vault(tmp).create("Case", premise="Callen Vane, a veteran pilot, digs out a buried starship.")
    sheet.save(c, START, 0, "start")
    c.append({"role": "user", "content": exchange[0]})
    c.append({"role": "assistant", "content": exchange[1]})
    await sheet.update(c, llm, router.router_system(c, c.load_state()))
    return sheet.load(c)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    a = ap.parse_args()
    llm = LLMClient(Settings.load().router)
    total = 0
    for name, exchange, ok in CASES:
        passed = 0
        for _ in range(a.runs):
            s = await run_case(llm, exchange)
            good = ok(s)
            passed += good
            if not good:
                print(f"   FAIL {name}: money={s['money']!r} gear={s['gear'][3:]} obl={s['obligations'][1:]}")
        total += passed
        print(f"{passed}/{a.runs}  {name}")
    print(f"\n{total}/{a.runs * len(CASES)} passed")


asyncio.run(main())
