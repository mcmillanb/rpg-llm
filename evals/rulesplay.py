"""Rules-mode playtest against a running server: an LLM player builds a character in session 0,
then plays, and the character sheet is recorded after every turn.

    uv run python evals/rulesplay.py --base http://localhost:8702 --system traveller --turns 20 --out tr.jsonl

The player rolls whenever the GM asks (the on-screen dice), answers session 0's questions and in
play picks fights, spends resources, buys things and rests, to exercise the sheet. Each line of
--out is one event: the player's move, the GM's reply, the sheet's counters, money and the
update's reported changes. Use a test vault: it creates and plays a real campaign.
"""

import argparse
import asyncio
import json
import re
import time

from rpg_llm.config import Settings
from rpg_llm.llm import NO_THINKING, LLMClient

SYSTEMS = {
    "daggerheart": ("Daggerheart", "A young ranger who grew up in the wild with a loyal wolf; wants a "
                    "bow and to protect the village of Thornwick from whatever stalks the forest."),
    "dnd": ("D&D 5e (2024 rules)", "A halfling rogue who was a pickpocket in a port city and now wants "
            "to make an honest name as an adventurer; quick, funny, a little greedy."),
    "traveller": ("Traveller", "A former scout who wants to see every world on the map; good with "
                  "ships and guns, owes money to the wrong people."),
    "starforged": ("Ironsworn: Starforged", "A courier pilot searching the Forge for her missing sister; "
                   "quick-witted, stubborn, and better with a ship than with people."),
    "ironsworn": ("Ironsworn", "A hunter from a small village who swore to track down the beast that "
                  "killed her brother; quiet, stubborn, deadly with a bow."),
    "discworld": ("Discworld: Adventures in Ankh-Morpork", "A junior wizard at Unseen University who is "
                  "better at dodging lectures than at magic, and keen to prove himself to the Archchancellor."),
}
# the dice tray's roll when the GM asks in words with no button
TRAY = {"starforged": "1D6", "ironsworn": "1D6", "discworld": "1D10"}

# play: every few turns a move that should change the sheet
PUSH_IRONSWORN = [
    "I swear an iron vow to see this through, whatever it takes.",
    "I attack the nearest enemy with everything I've got.",
    "We find somewhere safe, rest, and I tend my wounds.",
    "I set out on the journey to where the trail leads.",
    "I try to win this person's trust and make a real connection.",
    "I push on to finish what I started here.",
]
PUSH_DISCWORLD = [
    "I try something risky and clever to get past the problem, using my best Trait.",
    "If that went badly, I spend a Luck to get out of the Consequence.",
    "I try to talk my way past them, bluffing outrageously.",
    "I sneak in somewhere I'm definitely not allowed.",
    "I cast a small spell to sort this out.",
    "I have a real moment here: this is who I am. I want it as my Core.",
]
PUSH = [
    "I attack the nearest enemy with my best weapon.",
    "I use one of my special abilities or resources to get an edge.",
    "We find somewhere safe and rest properly for the night.",
    "I buy some supplies and a healing item with my money.",
    "I charge into danger to save someone, whatever it costs me.",
    "I patch up my wounds as best I can.",
]

PLAYER = """You are a player at a tabletop RPG table ({system}), talking to the game master.
Your character idea: {idea}
{phase}
Reply with ONLY what you say to the GM: 1-3 short sentences, first person. Make clear choices from
the options offered, never ask the GM to decide for you, and never write dice or dice results
yourself (the dice are rolled for you on screen)."""
SESSION0 = ("We're building your character (session 0). Answer the GM's question directly and pick "
            "from the options they list. When they ask for your backstory, give a short one. When "
            "everything's done and they offer ways to start, pick one.")
PLAY = ("The adventure is under way. Act boldly: fight, take risks, use your abilities and gear, "
        "spend money, and look after your wounds.")


def req(base, path, body=None, timeout=1800):
    import urllib.request
    r = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                               {"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    return urllib.request.urlopen(r, timeout=timeout)


def get(base, path):
    with req(base, path) as r:
        return json.load(r)


def stream(base, path, body):
    """One turn over SSE: the GM's text, rolls made, tools used, errors."""
    t = time.time()
    out, tools, rolls, err = "", [], [], None
    with req(base, path, body) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["type"] == "content":
                out += ev["text"]
            elif ev["type"] == "tool":
                tools.append(f"{ev['name']}({str(ev.get('arguments'))[:300]})")
            elif ev["type"] in ("roll", "rolled"):
                x = ev.get("roll") or ev
                rolls.append(f"{x.get('reason', '')}: {x.get('dice')}={x.get('total')}")
            elif ev["type"] == "error":
                err = ev["text"]
    return {"reply": out, "tools": tools, "rolls": rolls, "error": err, "seconds": round(time.time() - t, 1)}


def wait_idle(base, slug, limit=300):
    t = time.time()
    while time.time() - t < limit:
        st = get(base, f"/api/campaigns/{slug}/status")
        if not st.get("tracking") and not st.get("compacting"):
            break
        time.sleep(2)
    time.sleep(2)
    return get(base, f"/api/campaigns/{slug}/status")


def snapshot(base, slug):
    sh = get(base, f"/api/campaigns/{slug}/character")["sheet"] or {}
    return {"money": sh.get("money"), "resources": {r["name"]: f"{r['current']}/{r['max']}" + (" marked" if r.get("marks") else "")
                                                     for r in sh.get("resources") or []},
            "system": sh.get("system"), "gear": sh.get("gear"), "skills": sh.get("skills"),
            "condition": sh.get("condition")}


async def player(llm, system, idea, msgs, phase):
    convo = [{"role": "system", "content": PLAYER.format(system=system, idea=idea, phase=phase)}]
    for m in msgs[-6:]:
        if m.get("roll"):
            continue  # the dice are rolled on screen, not typed: keep them out of its examples
        convo.append({"role": "user" if m["role"] == "assistant" else "assistant", "content": (m.get("content") or "")[:3000]})
    if convo[-1]["role"] != "user":
        convo.append({"role": "user", "content": "(go on)"})
    msg = await llm.chat(convo, max_tokens=200, temperature=0.8, extra_body=NO_THINKING)
    return re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip() or "Yes, let's go on."


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8702")
    ap.add_argument("--system", choices=sorted(SYSTEMS), required=True)
    ap.add_argument("--turns", type=int, default=20, help="turns of play after session 0")
    ap.add_argument("--session0-limit", type=int, default=70)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    system, idea = SYSTEMS[a.system]
    log = open(a.out, "a")

    def record(kind, **data):
        log.write(json.dumps({"kind": kind, "t": round(time.time(), 1), **data}, ensure_ascii=False) + "\n")
        log.flush()

    base = a.base
    with req(base, "/api/campaigns", {"name": f"Test {a.system} {time.strftime('%H%M%S')}", "system": system,
                                      "premise": idea, "mode": "rules", "dice": "virtual"}) as r:
        slug = json.load(r)["slug"]
    record("created", slug=slug, system=system)
    llm = LLMClient(Settings.load().dm)

    async def step(text, phase_name):
        """One player action: a roll if the GM is waiting for one, else the player's words."""
        d = get(base, f"/api/campaigns/{slug}")
        last = d["messages"][-1] if d["messages"] else None
        rr = (last or {}).get("roll_request")
        # the GM asking the player to roll (not "I'll roll on the oracle", "roll the truths")
        asked = last and last["role"] == "assistant" and not rr and re.search(
            r"\b(?:please roll|roll\s*\+|(?:you|go ahead and|now) roll|roll (?:a|your|for|to)\b|make (?:a|an|the|your)\b.{0,30}\broll)",
            (last.get("content") or "").split("\n\n")[-1], re.I) and not re.search(
            r"\b(?:I(?:'ll| will| can)?|let me|me to) roll|roll (?:on|them|it) (?:the )?(?:oracle|table)", 
            (last.get("content") or "").split("\n\n")[-1], re.I)
        if rr:
            res = await asyncio.to_thread(stream, base, f"/api/campaigns/{slug}/roll", {"id": rr["id"]})
            said = f"[rolls {rr.get('dice')} for {rr.get('prompt', '')[:80]}]"
        elif asked and not text:  # asked in words with no button: the dice tray, as a player would
            tray = TRAY.get(a.system, "2D6")
            res = await asyncio.to_thread(stream, base, f"/api/campaigns/{slug}/roll-free",
                                          {"dice": tray, "reason": "the roll you asked for"})
            said = f"[dice tray: {tray}]"
        else:
            said = text or await player(llm, system, idea, d["messages"], SESSION0 if phase_name == "session0" else PLAY)
            res = await asyncio.to_thread(stream, base, f"/api/campaigns/{slug}/chat", {"content": said})
        st = await asyncio.to_thread(wait_idle, base, slug)
        snap = await asyncio.to_thread(snapshot, base, slug)
        record("turn", phase=phase_name, said=said, **res, sheet_changes=(st.get("last_sheet") or {}).get("changes"),
               sheet_at=(st.get("last_sheet") or {}).get("at"), status_error=st.get("error"), sheet=snap)
        print(f"[{a.system}] {phase_name} {res['seconds']:5.1f}s rolls={len(res['rolls'])} tools={[x.split('(')[0] for x in res['tools']]} "
              f"{'ERR ' + res['error'] if res['error'] else ''} | {snap['resources']} {snap['money']}", flush=True)

    await step("Let's start session 0: help me make my character.", "session0")
    n = 0
    while get(base, f"/api/campaigns/{slug}")["session0"] and n < a.session0_limit:
        await step(None, "session0")
        n += 1
    record("session0_done", steps=n, finished=not get(base, f"/api/campaigns/{slug}")["session0"])
    for i in range(1, a.turns + 1):
        pushes = PUSH_DISCWORLD if a.system == "discworld" else PUSH_IRONSWORN if a.system in TRAY else PUSH
        push = pushes[(i // 3) % len(pushes)] if i % 3 == 0 else None
        await step(push, "play")
    record("end", slug=slug, sheet=snapshot(base, slug))


asyncio.run(main())
