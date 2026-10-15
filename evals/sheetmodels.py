"""Replay a Rules-mode campaign's play through the sheet updater with a chosen model, to compare
models on the same exchanges.

    uv run python evals/sheetmodels.py --vault PATH --slug SLUG --model qwen3.5-9b --out 9b.jsonl

Starts from the sheet as it was when session 0 finished and feeds the exchanges after it one at
a time, as play would. Writes, per exchange, the reported changes and the counters, money and
gear after it. A copy of the campaign is used; the original isn't touched.
"""

import argparse
import asyncio
import dataclasses
import json
import shutil
import tempfile
from pathlib import Path

from rpg_llm import sheet
from rpg_llm.config import Settings
from rpg_llm.llm import LLMClient
from rpg_llm.vault import Vault


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    s = Settings.load()
    slot = dataclasses.replace(s.router, model=a.model)
    llm = LLMClient(slot)
    src = Path(a.vault) / "campaigns" / a.slug
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(src, tmp / "campaigns" / a.slug)
    c = Vault(tmp).get(a.slug)
    msgs = c.messages()
    # the sheet as session 0 left it: its last snapshot, and play from the message after it
    last0 = [h for h in sheet.history(c) if str(h.get("what", "")).startswith(("session 0", "blank"))][-1]
    base = last0["sheet"]
    start = next(i for i, m in enumerate(msgs) if m["id"] > last0["after"] and m["role"] == "user")
    all_lines = c.path("transcript.jsonl").read_text().splitlines()
    out = open(a.out, "w")
    sheet.save(c, base, msgs[start - 1]["id"] if start else 0, "replay start")
    i = start
    while i < len(msgs):
        j = i
        while j < len(msgs) and msgs[j]["role"] != "assistant":
            j += 1
        if j >= len(msgs):
            break
        c.path("transcript.jsonl").write_text("\n".join(all_lines[:j + 1]) + "\n")
        r = await sheet.update(c, llm)
        sh = sheet.load(c)
        row = {"after_id": msgs[j]["id"], "player": " | ".join((m.get("content") or "")[:200] for m in msgs[i:j]),
               "gm": (msgs[j].get("content") or "")[:600], "changes": (r or {}).get("changes"),
               "resources": {x["name"]: f"{x['current']}/{x['max']}" for x in sh.get("resources") or []},
               "money": sh.get("money"), "gear": sh.get("gear"), "condition": sh.get("condition")}
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        out.flush()
        print(row["after_id"], row["resources"], row["money"], (row["changes"] or [])[:3], flush=True)
        i = j + 1


asyncio.run(main())
