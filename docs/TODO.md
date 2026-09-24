# TODO

## Review later

- **Size of location/NPC notes.** Notes for places the player returns to often will keep gaining
  a dated entry per visit. Check how big they get in real play, and whether injection should use
  only the "Current state" summary or also roll up old entries.
- **Size of the campaign brief.** `brief.md` is always in context. Check it stays short in long
  campaigns.
- **Size of the router prompt.** The router's system prompt carries the whole gazetteer and scene
  index. In a long campaign (hundreds of entries) that could crowd the router's 62k window and
  slow cold calls; it may need trimming to recent/important entries.

## Not built yet (milestone 1 leftovers)

- **Pre-fetch.** The plan had the router guess the next turn's notes while the player reads. Not
  needed so far: with its prompt cached, the gatekeeper takes about 1.5 s.
- **Stop button** for a reply that's going wrong.
- **Settings screen.** Config is `.env` only.
- **Editing further back** than the last message.

## Router hardware

- The LM Studio box (`192.168.122.54`) prefills at ~220 tok/s against ~714 tok/s for the 27B
  on the V100s, which suggests it's partly on CPU. Cold router calls take ~13 s per 3k tokens;
  warm ones 1–2 s.
