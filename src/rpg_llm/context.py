"""Assembles the DM prompt.

Order is chosen for prompt caching: everything that is stable between turns comes first, and
the per-turn GM notes ride on the final user message, so the server only processes new text.
"""

import re

from rpg_llm import prompts
from rpg_llm.vault import Campaign, State, estimate_tokens


CONSEQUENCES = ("brutal", "normal", "low")
DICE_MODES = ("auto", "manual", "none")


def table(meta: dict) -> dict:
    """A campaign's table settings with defaults. Campaigns from before dice existed default to
    no dice, so an ongoing story doesn't suddenly start rolling."""
    c = meta.get("consequences")
    d = meta.get("dice")
    return {"consequences": c if c in CONSEQUENCES else "normal",
            "dice": d if d in DICE_MODES else "none"}


def system_prompt(campaign: Campaign, state: State) -> str:
    meta = campaign.meta
    system = meta.get("system") or ""
    extra = meta.get("dm_instructions") or ""
    t = table(meta)
    arc = re.sub(r"\n*<!--.*?-->\s*$", "", campaign.read("arc.md"), flags=re.S).strip()
    text = prompts.DM_SYSTEM.format(
        system_line=f"\nGame system / setting: {system}\n" if system else "",
        extra=f"\nAdditional instructions from the player:\n{extra}\n" if extra else "",
        table_rules=prompts.CONSEQUENCES[t["consequences"]] + "\n\n" + prompts.DICE[t["dice"]],
        brief=campaign.brief.strip() or "(nothing yet)",
        arc=(f"\n# Story arc (GM only: guidance, not a script; never reveal it)\n\n{arc}\n"
             if arc else ""),
    )
    if state.fold:
        text += f"\n# Earlier in this session (condensed)\n\n{state.fold['summary']}\n"
    return text


def live_tail(state: State, messages: list[dict]) -> list[dict]:
    start = state.live_start()
    until = state.fold["until"] if state.fold else 0
    return [m for m in messages if m["id"] >= start and m["id"] > until]


def build(campaign: Campaign, state: State, messages: list[dict],
          gm_notes: str | None = None) -> list[dict]:
    out = [{"role": "system", "content": system_prompt(campaign, state)}]
    for m in live_tail(state, messages):
        if out[-1]["role"] == m["role"]:  # keep strict alternation for chat templates
            out[-1]["content"] += "\n\n" + m["content"]
        else:
            out.append({"role": m["role"], "content": m["content"]})
    if gm_notes and out[-1]["role"] == "user":
        out[-1]["content"] = f"[GM NOTES]\n{gm_notes}\n[/GM NOTES]\n\n{out[-1]['content']}"
    return out


def tail_tokens(campaign: Campaign, state: State, messages: list[dict]) -> int:
    return estimate_tokens(system_prompt(campaign, state)) + sum(
        estimate_tokens(m["content"]) for m in live_tail(state, messages))
