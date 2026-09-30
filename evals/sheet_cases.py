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

START_GEAR = ["Work Gloves", "Basic Hand Tools", "Thermal Reseal Compound",
              "3 military-grade power cells", "Ben Vane's logbook"]
START = {"name": "Callen Vane", "concept": "Disabled Navy veteran, intuitive pilot",
         "money": "204 Cr", "gear": START_GEAR,
         "assets": ["Military utility truck", "Starship Aethelgard (buried)"],
         "obligations": ["Complete atmospheric test flight within three months",
                         "Pay Darrow 50% of the power cells' sale price"]}

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


def one_cell(s):
    cells = [g.lower() for g in s["gear"] if "cell" in g.lower()]
    return len(cells) == 1 and not re.search(r"\b(2|3|two|three)\b", cells[0])


def has(items, *words):
    return any(w in i.lower() for i in items for w in words)


FOUND = ("50-50 as agreed, lets open the box",
         "You pry the lid free. Inside, packed in yellowed foam, are three pristine military-grade "
         "power cells and a leather-bound logbook with Ben Vane's name on it. You lift the power "
         "cells out into your work gloves and flip open the logbook. \"I'll take my half in cash "
         "when we sell these cells,\" Darrow says.")
OFFERED = ("knock and enter",
           "You set the three power cells on Kess's desk. He taps one against his palm. \"High-"
           "grade naval surplus.\" He scribbles on a datapad and slides it across. \"Two hundred "
           "credits total. Fair market value.\" The cash is ready to hand over: two hundred more in "
           "your account once these are transferred.")

SOLD = ("Deal, I'll take the two hundred.",
        "Kess counts two hundred credits into your hand and sweeps the three power cells into his "
        "desk drawer. \"Pleasure doing business.\"")

PAID_PART = [("agreed, 130cr for the cells, plus the original 81cr for collecting the buoy. Take the money.",
              "Kess nods and slides two of the power cells into his desk drawer, leaving the third with "
              "you. He counts out two hundred and eleven credits: 130 for the cells and 81 for the buoy "
              "run. \"Darrow is waiting outside for his share. I suggest you settle him before he "
              "starts counting my change.\"")]
CORRECTED = [("agreed, 130cr for the cells, plus the original 81cr for collecting the buoy. Take the money.",
              "Kess slides two of the cells into his drawer and counts out a neat pile for you: two "
              "hundred credits total to cover the power cells and your hazard pay for the buoy run."),
             ("200cr? 130cr + 81cr is 211cr",
              "Kess pauses. \"Right,\" he chuckles, adding a few more credit slips to the stack until "
              "it reaches the correct sum of two hundred and eleven credits.")]

ON_DELIVERY = ("I'll take it",
               "The clerk slides three forms across the glass. \"The crates are waiting in Loading Bay "
               "C. Just sign off with the supervisor at the intake valve when you get there. He has "
               "the cash.\" Twenty-five credits on delivery.")
THEIRS = ("Pull it up",
          "Darrow hauls the dripping military canister over the gunwale. It's right there in front of "
          "you: military surplus that could easily be worth more than the fifty credits Darrow just "
          "earned for the tow.")

RENTAL_QUOTE = ("4 days of crawler hire, i cant afford the week",
                "Marta taps her chin. \"I can squeeze four days out of the schedule for you. That'll be "
                "**60 credits a day**.\" She slides a worn contract across the counter and rests her hand "
                "on it. \"**240 credits total.** That leaves you with enough for fuel and instant coffee. "
                "Do we have a deal?\"")
NO_MONEY_AFTER_PAY = [("I hand over the valve",
                       "Kess reaches into his drawer and slides a neat stack of twenty credits across the "
                       "desk toward you. \"Good work.\""),
                      ("TBH, I'm struggling to get the ship out, any suggestions?",
                       "Kess leans back. \"Marta is solid. She rents out clean gear and keeps her mouth "
                       "shut. Or there's a salvage contractor down at the scrap yards.\" Do you want his "
                       "contact, or will you trust Marta?")]
PAID_DARROW = ("Head outside and give Darrow 100cr, \"That's half the value of the cells, I decided to keep "
               "one for myself, so I only got 30cr and kept the cell\" Then head out to find the valve",
               "You step outside where Darrow is leaning against a crate. You press the stack of credits "
               "into his hand. \"That's half the value of the cells,\" you say. \"I decided to keep one "
               "for myself.\" Darrow counts it and claps you on the shoulder. You climb back into the "
               "truck and head for Secondary Road 4.")

PLANNED = ("lets do both, I'll sell those to you and I will do the contract. make the exchange, load up and head to Silas",
           "Kess nods approvingly at the scrap. \"Twenty credits,\" he says, handing over a small stack "
           "of chits. He keeps your registration certificate on his desk as collateral until the books "
           "balance. You load three crates of sensor housing parts for Silas. The math is simple: the "
           "twenty credits from the scrap plus the eighty credit hauling fee leaves you owing only "
           "sixteen more once the job is done, meaning you'll walk away fully registered.")


CASES = [
    ("found and kept: cells in gear", FOUND,
     lambda s: money(s) == 204 and has(s["gear"], "power cell", "cell")),
    ("sold two of three, paid, Darrow not paid yet", PAID_PART,
     lambda s: money(s) == 415 and one_cell(s) and has(s["obligations"], "darrow")),
    # the first exchange is already on the sheet (404 = 204 + the 200 narrated); only the
    # correction is applied
    ("a correction adds only the difference", CORRECTED,
     lambda s: money(s) == 415),
    ("a rental quoted: not paid until agreed", RENTAL_QUOTE, lambda s: money(s) == 204),
    ("the previous payment isn't applied again", NO_MONEY_AFTER_PAY, lambda s: money(s) == 224),
    ("paid Darrow his share, kept the cell", PAID_DARROW,
     lambda s: money(s) == 104 and has(s["gear"], "cell") and not has(s["obligations"], "darrow")),
    ("paid now for scrap; the haul fee and the balance come later", PLANNED, lambda s: money(s) == 224),
    ("paid on delivery: nothing yet", ON_DELIVERY, lambda s: money(s) == 204),
    ("someone else's money isn't yours", THEIRS, lambda s: money(s) == 204),
    ("a price offered: no sale yet", OFFERED,
     lambda s: money(s) == 204),
    ("offer accepted and paid: money up", SOLD,
     lambda s: money(s) == 404),
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
    pairs = exchange if isinstance(exchange, list) else [exchange]
    if exchange is NO_MONEY_AFTER_PAY:  # the valve payment is already on the sheet
        sheet.save(c, {**START, "money": "224 Cr"}, 0, "start")
        c.append({"role": "user", "content": pairs[0][0]})
        c.append({"role": "assistant", "content": pairs[0][1]})
        pairs = pairs[1:]
    if exchange is CORRECTED:  # start with the first exchange already applied
        sheet.save(c, {**START, "money": "404 Cr",
                       "gear": [g for g in START_GEAR if "cell" not in g] + ["1 military-grade power cell"]},
                   0, "start")
        c.append({"role": "user", "content": pairs[0][0]})
        c.append({"role": "assistant", "content": pairs[0][1]})
        pairs = pairs[1:]
    for player, gm in pairs:  # each exchange applied in turn, as in play
        c.append({"role": "user", "content": player})
        c.append({"role": "assistant", "content": gm})
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
