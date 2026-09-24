# TODO

## Review later

- **Size of location/NPC notes.** Notes for places the player returns to often will keep gaining
  a dated entry per visit. Check how big they get in real play, and whether injection should use
  only the "Current state" summary or also roll up old entries.
- **Size of the campaign brief.** `brief.md` is always in context. Check it stays short in long
  campaigns.

## Router endpoint

- The router is meant to use LM Studio at `http://192.168.122.54:8888/v1` with `qwen/qwen3-vl-8b`
  (commented out in `.env`). On 2026-09-24 LM Studio failed to load the model with "System
  resources observer shutdown requested", so the router falls back to the DM model for now.
- Context-window detection only understands llama.cpp (`meta.n_ctx`). LM Studio reports it at
  `/api/v0/models` (`loaded_context_length`, then `max_context_length`); add that.
