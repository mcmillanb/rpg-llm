"""Long automated playtest against a running server: an LLM plays the player.

    uv run python evals/soak.py --base http://localhost:8702 --turns 100 --out soak.jsonl

Creates a fresh campaign, then plays. Every few turns the player is nudged to travel; now and
then it deliberately goes back somewhere it has been (to exercise recall); twice it takes a
message back mid-reply (Esc); every --session-every turns it forces filing, like a break between
sessions. Writes one JSON line per event and prints a summary at the end.

Use a test vault: it creates and plays a real campaign on whatever server --base points at.
"""

import argparse
import asyncio
import json
import random
import re
import statistics
import time
import urllib.request

from rpg_llm.config import Settings
from rpg_llm.llm import NO_THINKING, LLMClient

PREMISE = ("Tomas Reyes is a former Scout Service courier, now a freelance trader with a tired "
           "Type-A free trader, the Lantern, and a crew of one: Ilse, a sharp-tongued engineer. "
           "He carries a sealed diplomatic pouch he was paid to deliver to a noble on Regina, "
           "but the noble's household swears it was never sent. The story opens at the downport "
           "of Mora, where customs has just flagged the Lantern for inspection.")

TRAVEL = [
    "I head back to the Lantern and tell Ilse to prep for lift-off.",
    "I take the tram up to the orbital highport to look for cargo.",
    "We lift off and jump for the next system on our route.",
    "I go down into the startown market to find a buyer and a drink.",
    "I find a cheap room in the startown and get some sleep; next morning I get moving.",
]

PLAYER = """You are playing the player character in a solo tabletop RPG run by a game master.
Character: {premise}
Reply with ONLY your character's next action or words: 1-3 sentences, first person, decisive.
Engage with what the GM just described. {nudge}"""


def req(base, path, body=None, method=None, timeout=900):
    r = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                               {"Content-Type": "application/json"},
                               method=method or ("POST" if body is not None else "GET"))
    return urllib.request.urlopen(r, timeout=timeout)


def get(base, path):
    with req(base, path) as r:
        return json.load(r)


def turn(base, slug, text, abort_after=None):
    """Play one turn over SSE. abort_after: close the connection after N seconds (Esc)."""
    t = time.time()
    out, ev_ctx, rolls, tools, err, done = "", None, [], [], None, None
    with req(base, f"/api/campaigns/{slug}/chat", {"content": text}) as r:
        for line in r:
            if abort_after and time.time() - t > abort_after:
                break
            line = line.decode().strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["type"] == "content":
                out += ev["text"]
            elif ev["type"] == "context":
                ev_ctx = ev
            elif ev["type"] == "roll":
                rolls.append(ev)
            elif ev["type"] == "tool":
                tools.append(f"{ev['name']}({ev['arguments']})")
            elif ev["type"] == "error":
                err = ev["text"]
            elif ev["type"] == "done":
                done = ev["message"]
    return {"text": text, "reply": out, "seconds": round(time.time() - t, 1), "error": err,
            "injected": (ev_ctx or {}).get("injected"), "rolls": [
                f"{x['reason']}: {x['dice']}={x['total']}" + (f" vs {x['target']} {'ok' if x['success'] else 'fail'}" if "target" in x else "")
                for x in rolls],
            "tools": tools, "stats": (done or {}).get("stats"), "aborted": bool(abort_after)}


def wait_idle(base, slug, extra=3.0, limit=180):
    t = time.time()
    while time.time() - t < limit:
        st = get(base, f"/api/campaigns/{slug}/status")
        if not st["tracking"] and not st["compacting"]:
            break
        time.sleep(1)
    time.sleep(extra)
    return get(base, f"/api/campaigns/{slug}/status")


async def player_move(llm, history, nudge):
    convo = [{"role": "system", "content": PLAYER.format(premise=PREMISE, nudge=nudge)}]
    for m in history[-6:]:
        convo.append({"role": "user" if m["role"] == "assistant" else "assistant",
                      "content": m["content"][:2500]})
    if convo[-1]["role"] != "user":
        convo.append({"role": "user", "content": "(continue)"})
    msg = await llm.chat(convo, max_tokens=160, temperature=0.9, extra_body=NO_THINKING)
    return re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()


def campaign_stats(base, slug, vault_root=None):
    st = get(base, f"/api/campaigns/{slug}/status")
    brief = get(base, f"/api/campaigns/{slug}/file?path=brief.md")["text"]
    sheet = get(base, f"/api/campaigns/{slug}/character")["sheet"]
    return {"scenes": len(st["scenes"]), "filed": sum(s["status"] == "compacted" for s in st["scenes"]),
            "wiki_entries": st["gazetteer_size"], "brief_words": len(brief.split()),
            "sheet_money": (sheet or {}).get("money"), "sheet_gear": len((sheet or {}).get("gear", []))}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8702")
    ap.add_argument("--turns", type=int, default=100)
    ap.add_argument("--session-every", type=int, default=30)
    ap.add_argument("--out", default="soak.jsonl")
    ap.add_argument("--slug")
    a = ap.parse_args()
    random.seed(7)
    log = open(a.out, "a")

    def record(kind, **data):
        row = {"kind": kind, "t": round(time.time(), 1), **data}
        log.write(json.dumps(row, ensure_ascii=False) + "\n")
        log.flush()
        return row

    base = a.base
    if a.slug:
        slug = a.slug
    else:
        with req(base, "/api/campaigns", {"name": f"Soak {time.strftime('%H%M')}", "premise": PREMISE,
                                          "system": "Traveller (Third Imperium)",
                                          "consequences": "normal", "dice": "auto"}) as r:
            slug = json.load(r)["slug"]
        record("created", slug=slug)
        time.sleep(90)  # starting sheet + story arc are written in the background

    llm = LLMClient(Settings.load().dm)
    history = get(base, f"/api/campaigns/{slug}")["messages"]
    unsend_at = {12, 47}
    for n in range(1, a.turns + 1):
        nudge = ""
        places = [e for e in (await asyncio.to_thread(get, base, f"/api/campaigns/{slug}/file?path=gazetteer.yaml"))["text"].split("\n- name: ") if "type: location" in e]
        if n % 25 == 0 and places:
            name = places[random.randrange(len(places))].split("\n")[0].replace("- name: ", "").strip()
            move = f"I decide to head back to {name}; there's something there I need to deal with."
        elif n % 6 == 0:  # a concrete move: nudges alone let the simulated player stay put
            move = f"That's enough here. {TRAVEL[(n // 6) % len(TRAVEL)]}"
        else:
            move = await player_move(llm, history, nudge)
        if n in unsend_at:
            r = turn(base, slug, move, abort_after=4)
            with req(base, f"/api/campaigns/{slug}/unsend", {}) as resp:
                back = json.load(resp)
            record("unsend", turn=n, text=move, got_back=back.get("content") == move)
            time.sleep(2)
        r = turn(base, slug, move)
        st = wait_idle(base, slug)
        row = record("turn", n=n, **r, scene=st["scenes"][-1]["id"],
                     location=st["scenes"][-1].get("location"),
                     sheet=(st.get("last_sheet") or {}).get("changes"),
                     verdict=(st.get("last_verdict") or {}).get("transition"))
        s = r["stats"] or {}
        print(f"T{n:3} {r['seconds']:5.1f}s ctx={s.get('prompt_tokens')} cached={round(100*s.get('cached_tokens',0)/max(1,s.get('prompt_tokens',1)))}% "
              f"scene={row['scene']} inj={len(r['injected'] or [])} rolls={len(r['rolls'])} tools={len(r['tools'])} "
              f"{'ERR ' + r['error'] if r['error'] else ''}", flush=True)
        history = get(base, f"/api/campaigns/{slug}")["messages"]
        if n % a.session_every == 0:
            t = time.time()
            with req(base, f"/api/campaigns/{slug}/compact", {}, timeout=3600) as resp:
                rep = json.load(resp)
            cs = campaign_stats(base, slug)
            record("session_break", after_turn=n, seconds=round(time.time() - t, 1),
                   filed_now=len(rep.get("filed", [])), arc=rep.get("arc"), audit=len(rep.get("audit", [])), **cs)
            print(f"   == session break: filed {len(rep.get('filed', []))} scenes in {time.time()-t:.0f}s, arc={rep.get('arc')}, {cs}", flush=True)

    turns = [json.loads(l) for l in open(a.out) if '"kind": "turn"' in l]
    secs = [t["seconds"] for t in turns]
    ctx = [t["stats"]["prompt_tokens"] for t in turns if t.get("stats")]
    cached = [t["stats"]["cached_tokens"] / t["stats"]["prompt_tokens"] for t in turns if t.get("stats") and t["stats"]["prompt_tokens"]]
    record("summary", slug=slug, turns=len(turns), errors=sum(bool(t["error"]) for t in turns),
           median_seconds=statistics.median(secs), p90_seconds=sorted(secs)[int(len(secs) * .9) - 1],
           median_ctx=statistics.median(ctx), max_ctx=max(ctx), median_cached=round(statistics.median(cached), 2),
           rolls=sum(len(t["rolls"]) for t in turns), lookups=sum(len(t["tools"]) for t in turns),
           recalls=sum(bool(t["injected"]) for t in turns), **campaign_stats(base, slug))
    print(open(a.out).read().splitlines()[-1])


asyncio.run(main())
