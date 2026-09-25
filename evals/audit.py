"""Check the hindsight re-check of scene boundaries (run before filing) on real boundaries.

    uv run python evals/audit.py

fixtures/soak: four boundaries the tracker drew during a soak test, labelled by hand: three
wrong (moving within one building, a move the player announced but the GM never narrated, and
walking down to the bays of the same station) and one right (the lift down to the startown).
fixtures/targeted: four real moves from a scripted run (bar -> docked ship, a jump, orbit ->
the planet's startown, back to the ship), all to keep.
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

FIXTURES = [Path(__file__).parent / "fixtures" / name for name in ("soak", "targeted")]


async def main() -> None:
    llm = LLMClient(Settings.load().router)
    ok = total = 0
    for fixture in FIXTURES:
        print(f"== {fixture.name}")
        for case in json.loads((fixture / "boundaries.json").read_text()):
            # one boundary at a time, so each is judged against the right "before" scene
            tmp = Path(tempfile.mkdtemp())
            root = tmp / "campaigns" / fixture.name
            shutil.copytree(fixture, root)
            c = Vault(tmp).get(fixture.name)
            c.save_state(State(scenes=[Scene(1, 1, "closed_provisional", location=case["before"]),
                                       Scene(2, case["user_id"], location=case["location"])]))
            r = (await router.audit(c, llm))[0]
            got = "keep" if r["keep"] else "undo"
            ok += got == case["expect"]
            total += 1
            print(f"{'ok ' if got == case['expect'] else 'BAD'} boundary at {case['user_id']} "
                  f"({case['before']} -> {case['location']}): expect {case['expect']}, got {got} "
                  f"({r['why']}) | move={r['movement_quote'][:60]!r}")
    print(f"{ok}/{total} correct")


asyncio.run(main())
