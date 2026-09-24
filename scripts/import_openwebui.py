"""Import an Open WebUI chat export as a new campaign, then segment it into scenes and file
everything except the last scene into the wiki.

    uv run python scripts/import_openwebui.py export.json [--chat N] [--name NAME]
        [--system "Traveller (Mongoose 2e)"] [--premise-file premise.md] [--no-file]

Run with the server stopped. Segmenting and filing are resumable: if interrupted, run
    uv run python scripts/import_openwebui.py --resume <campaign-slug>
"""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from rpg_llm import compactor, router
from rpg_llm.config import Settings
from rpg_llm.importers import openwebui
from rpg_llm.llm import LLMClient
from rpg_llm.vault import Vault


async def process(s: Settings, campaign, file_scenes: bool) -> None:
    router_llm, archiver = LLMClient(s.router), LLMClient(s.archiver)
    t = time.time()

    def progress(n, total, v):
        mark = "NEW SCENE" if v.get("transition") and v.get("confidence", 0) >= s.router_threshold else ""
        print(f"  [{n}/{total}] {time.time() - t:6.0f}s {mark} {v.get('new_location') or ''}", flush=True)

    print("Segmenting into scenes…")
    opened = await router.backfill(campaign, router_llm, s.router_threshold, progress)
    print(f"  {opened} scene boundaries found")
    if file_scenes:
        print("Filing closed scenes into the wiki (the last scene stays live)…")
        report = await compactor.compact(campaign, router_llm, archiver, asyncio.Lock())
        print(json.dumps({k: report[k] for k in ("filed", "seconds")}, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("export", nargs="?", type=Path)
    ap.add_argument("--chat", type=int, help="which chat in a multi-chat export (see list)")
    ap.add_argument("--name")
    ap.add_argument("--system", default="")
    ap.add_argument("--premise-file", type=Path)
    ap.add_argument("--no-file", action="store_true", help="segment only, don't run the archiver")
    ap.add_argument("--resume", metavar="SLUG")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    s = Settings()
    vault = Vault(s.vault_path)

    if a.resume:
        asyncio.run(process(s, vault.get(a.resume), not a.no_file))
        return
    chats = openwebui.load_chats(a.export)
    if len(chats) > 1 and a.chat is None:
        for i, c in enumerate(chats):
            print(f"{i}: {c['_title']} ({len(openwebui.branch(c))} messages)")
        print("Pick one with --chat N")
        return
    chat = chats[a.chat or 0]
    premise = a.premise_file.read_text() if a.premise_file else ""
    campaign = openwebui.import_chat(vault, chat, a.name, a.system, premise)
    print(f"Imported {len(campaign.messages())} messages into campaign '{campaign.slug}'")
    asyncio.run(process(s, campaign, not a.no_file))


main()
