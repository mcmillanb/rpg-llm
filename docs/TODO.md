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
- **Editing further back** than the last message.

## Router hardware

- Router moved to `qwen3.5-9b` on the inference box (2026-09-25): scene checks 1.3–2.2 s, recall
  checks about 1 s. The earlier LM Studio setup on the Mac Studio took 4–7 s warm, 13 s cold.

## Seen in playtesting (2026-09-24)

- **Gatekeeper still adds some unneeded notes in live play** (e.g. the ship's note when you
  mention jumping). The cost is small (a few hundred tokens), but the "📜 from archive" chip and
  the Archive drawer show what was added, so keep an eye on it.
- **Long thinking.** The 27B sometimes reasons for 30–40 s before writing. Unticking "let the model think"
  for the game master in admin turns it off if the replies stay good without it.
- **Very long scenes on import.** The archiver sees at most ~250k characters of a scene
  (the end is kept). A huge single scene in an imported chat would lose its beginning in the
  wiki (the transcript keeps everything). Chunked summarising would fix it if it comes up.
- **Test campaigns** in `vault/campaigns/` (`test-traveller`, `imported-traveller`,
  `autoplay-traveller`) are from prototyping; delete them whenever.
