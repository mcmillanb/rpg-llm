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
