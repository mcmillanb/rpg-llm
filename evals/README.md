# Router evals

Small, labelled checks for the router prompts, run against the real models in `.env`.
Re-run them after changing `prompts.py` or switching the router model.

    uv run python evals/track.py        # scene-transition detection, per exchange
    uv run python evals/gatekeep.py     # which wiki notes get pulled in for a message

`fixtures/traveller/` is a short test campaign (13 exchanges, first two scenes filed) played
on the prototype on 2026-09-24. Results then with `qwen/qwen3-vl-8b` as router: track 13/13,
gatekeep 6/6. With `fixtures/autoplay` added (12 more exchanges, one long bar stand-off), track 25/25 (24/25 after the site-level location change: returning to a docked ship is borderline).
