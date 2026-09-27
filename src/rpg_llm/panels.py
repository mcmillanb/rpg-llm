"""The play page's side panels: what the player's character knows, gathered for display.

Character and Gear are views of the sheet, People is the cast, Places comes from scene tracking
and the location notes, and the Journal from the sheet's obligations, the brief's threads and
the timeline. Nothing here reads the story arc: that is the GM's secret.
"""

import hashlib
import json
import re

from rpg_llm import cast, portrait, sheet, wiki
from rpg_llm.vault import Campaign

CHARACTER = ("name", "concept", "appearance", "skills", "condition", "companions")
GEAR = ("money", "gear", "assets")
JOURNAL = ("obligations",)


def section(markdown: str, heading: str) -> str:
    m = re.search(rf"^##\s*{re.escape(heading)}[^\n]*\n(.*?)(?=^##\s|\Z)", markdown, re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def places(campaign: Campaign) -> dict:
    state = campaign.load_state()
    gazetteer = campaign.gazetteer()
    visits: dict[str, dict] = {}
    for s in state.scenes:
        name = (s.location or "").strip()
        if not name:
            continue
        v = visits.setdefault(name.lower(), {"name": name, "visits": 0, "first_scene": s.id})
        v["visits"] += 1
        v["last_scene"] = s.id
    matched = set()
    for v in visits.values():
        e = wiki.find(gazetteer, v["name"], "location")
        if e is not None and e["type"] == "location":
            matched.add(e["path"])
            v.update(summary=wiki.current_state(campaign, e["path"]), path=e["path"])
    known = [{"name": e["name"], "summary": wiki.current_state(campaign, e["path"]), "path": e["path"]}
             for e in gazetteer if e["type"] == "location" and e["path"] not in matched]
    here = (state.current.location or "").strip() if state.scenes else ""
    for p in cast.people(campaign):  # who is where, when the cast says so
        for v in [*visits.values(), *known]:
            if p["where"] and v["name"].lower() in p["where"].lower():
                v.setdefault("people", []).append(p["name"])
    return {"current": here,
            "visited": sorted(visits.values(), key=lambda v: -v["last_scene"]),
            "known": known}


TIMELINE = re.compile(r"\|Scene (\d+): ([^\]]+)\]\](?: at \[\[[^|\]]*\|([^\]]+)\]\])?:\s*(.*)")


def journal(campaign: Campaign) -> dict:
    state = campaign.load_state()
    brief = campaign.brief
    story = []
    for line in campaign.read("timeline.md").splitlines():
        m = TIMELINE.search(line)
        if m:
            story.append({"scene": int(m.group(1)), "title": m.group(2), "location": m.group(3) or "",
                          "summary": m.group(4).strip()})
    filed = {s["scene"] for s in story}
    story += [{"scene": s.id, "title": s.title or "", "location": s.location or "", "summary": "",
               "live": True} for s in state.scenes if s.id not in filed]
    earlier = ""
    if state.fold:  # the condensed start of the current stretch, minus its cast list
        earlier = re.sub(r"^Cast:\n(?:[-*].*\n?)+\s*", "", state.fold["summary"].strip()).strip()
    sh = sheet.load(campaign) or {}
    return {"obligations": sh.get("obligations", []),
            "threads": section(brief, "Active threads"),
            "situation": section(brief, "Situation"),
            "earlier": earlier,
            "as_of": max((s.id for s in state.scenes if s.status == "compacted"), default=0),
            "unfiled": sum(s.status != "compacted" for s in state.scenes),
            "story": story}


def _sig(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def signatures(campaign: Campaign) -> dict:
    """A short fingerprint per panel, so the page can mark the ones that changed since the
    player last looked."""
    sh = sheet.load(campaign) or {}
    state = campaign.load_state()
    return {
        "character": _sig([sh.get(k) for k in CHARACTER]),
        "gear": _sig([sh.get(k) for k in GEAR]),
        "people": _sig([[(p["name"], p["pronouns"], p["role"], p["where"], p["standing"],
                          p["notes"]) for p in cast.people(campaign)],
                        portrait.people_index(campaign)]),
        "places": _sig([(s.location or "") for s in state.scenes]),
        "journal": _sig([sh.get("obligations"), section(campaign.brief, "Active threads"),
                         len(state.scenes)]),
    }
