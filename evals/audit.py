"""Check the hindsight re-check of scene boundaries (run before filing) on real boundaries.

    uv run python evals/audit.py

fixtures/soak: four boundaries the tracker drew during a soak test, labelled by hand: three
wrong (moving within one building, a move the player announced but the GM never narrated, and
walking down to the bays of the same station) and one right (taking the lift down to the
startown market).
"""

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rpg_llm import router  # noqa: E402
from rpg_llm.config import Settings  # noqa: E402
from rpg_llm.llm import LLMClient  # noqa: E402
from rpg_llm.vault import Scene, State, Vault  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "soak"


async def main() -> None:
    llm = LLMClient(Settings.load().router)
    cases = json.loads((FIXTURE / "boundaries.json").read_text())
    ok = 0
    for case in cases:
        # one boundary at a time, so each is judged against the right "before" scene
        tmp = Path(tempfile.mkdtemp())
        root = tmp / "campaigns" / "soak"
        shutil.copytree(FIXTURE, root)
        c = Vault(tmp).get("soak")
        c.save_state(State(scenes=[Scene(1, 1, "closed_provisional", location="Mora downport"),
                                   Scene(2, case["user_id"], location=case["location"])]))
        r = (await router.audit(c, llm))[0]
        got = "keep" if r["keep"] else "undo"
        ok += got == case["expect"]
        print(f"{'ok ' if got == case['expect'] else 'BAD'} boundary at {case['user_id']} "
              f"({case['location']}): expect {case['expect']}, got {got} ({r['why']}) "
              f"| move={r['movement_quote'][:70]!r} back={r['return_quote'][:40]!r}")
    print(f"{ok}/{len(cases)} correct")


asyncio.run(main())
