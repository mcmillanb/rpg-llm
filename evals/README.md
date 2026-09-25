# Router evals

Small, labelled checks for the router prompts, run against the real models in `.env`.
Re-run them after changing `prompts.py` or switching the router model.

    uv run python evals/track.py        # scene-transition detection, per exchange
    uv run python evals/gatekeep.py     # which wiki notes get pulled in for a message

`fixtures/traveller/` is a short test campaign (13 exchanges, first two scenes filed) played
on the prototype on 2026-09-24. Results then with `qwen/qwen3-vl-8b` as router: track 13/13,
gatekeep 6/6. With `fixtures/autoplay` added (12 more exchanges, one long bar stand-off), track 25/25 (24/25 after the site-level location change: returning to a docked ship is borderline).

2026-09-25, router `qwen3.5-9b` (llama.cpp on the inference box, thinking off): track 25/25 at
1.3–2.2 s per check (the VL-8B on LM Studio took 4–7 s), gatekeep 6/6 at about 1 s. The 9B
over-picked "related" notes at first; notes for anything already in play (named in the GM's
last reply, or the current location) are now filtered in code.

2026-09-25, after the soak and a scripted filing test (router `qwen3.5-9b`):
- track 32/35 over three fixtures. Misses: a move to a docked ship caught one exchange late (2
  cases), and an overnight rest the 9B won't quote from the GM's narration.
- audit (`evals/audit.py`) 7/8. It now undoes a boundary only on hard evidence (no narrated move
  or time skip, or going straight back); the one miss is the resulting trade-off: a move into a
  back room is kept as a small extra scene rather than merged.
- gatekeep 6/6.
- `evals/sheet.py`: money stays numeric through 31 real exchanges.
