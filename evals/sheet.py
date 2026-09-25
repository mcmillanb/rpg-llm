"""Replay a campaign's exchanges through the character-sheet updater and watch the sheet.

    uv run python evals/sheet.py            # fixture: 31 exchanges of a customs stand-off

Checks the failure modes seen in play: money turning into a note ("unknown ..."), debts filed
as money, and the sheet swelling into a story log.
"""

import asyncio
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from rpg_llm import router, sheet  # noqa: E402
from rpg_llm.config import Settings  # noqa: E402
from rpg_llm.llm import LLMClient  # noqa: E402
from rpg_llm.vault import Vault  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "customs"


async def main() -> None:
    llm = LLMClient(Settings.load().router)
    tmp = Path(tempfile.mkdtemp())
    root = tmp / "campaigns" / "customs"
    root.mkdir(parents=True)
    for f in ("campaign.yaml", "brief.md"):
        shutil.copy(FIXTURE / f, root / f)
    c = Vault(tmp).get("customs")
    msgs = [json.loads(line) for line in (FIXTURE / "transcript.jsonl").read_text().splitlines()]
    sheet.save(c, yaml.safe_load((FIXTURE / "start_sheet.yaml").read_text()), 0, "start")
    print("start money:", sheet.load(c)["money"])
    lines = ""
    bad_money = 0
    for i in range(0, len(msgs) - 1, 2):
        lines += "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs[i:i + 2])
        c.path("transcript.jsonl").write_text(lines)
        t = time.time()
        r = await sheet.update(c, llm, router.router_system(c, c.load_state()))
        s = sheet.load(c)
        bad_money += not re.search(r"\d", s["money"])
        sizes = {k: len(s[k]) for k in sheet.LISTS}
        print(f"ex{i // 2 + 1:<2} {time.time() - t:4.1f}s money={s['money']!r:14} "
              f"cond={sizes['condition']} gear={sizes['gear']} obl={sizes['obligations']} "
              f"| {'; '.join((r or {}).get('changes', []))[:90]}")
    s = sheet.load(c)
    print("\nfinal sheet:\n" + yaml.safe_dump(s, sort_keys=False, allow_unicode=True))
    print(f"exchanges with non-numeric money: {bad_money}")


asyncio.run(main())
