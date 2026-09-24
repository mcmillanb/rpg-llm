"""Rebuild a campaign's wiki from its transcript (keeps scene boundaries).

    uv run python scripts/rebuild_wiki.py <campaign-slug>

Stop the server first, or it may write to the campaign at the same time.
"""

import asyncio
import json
import logging
import sys

from rpg_llm import compactor
from rpg_llm.config import Settings
from rpg_llm.llm import LLMClient
from rpg_llm.vault import Vault


async def main(slug: str) -> None:
    s = Settings()
    c = Vault(s.vault_path).get(slug)
    compactor.reset_wiki(c)
    report = await compactor.compact(c, LLMClient(s.router), LLMClient(s.archiver), asyncio.Lock())
    print(json.dumps(report, indent=2))


logging.basicConfig(level=logging.INFO)
asyncio.run(main(sys.argv[1]))
