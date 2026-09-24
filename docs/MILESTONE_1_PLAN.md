# Milestone 1 Plan: Minimal Working Loop

> **Status 2026-09-24:** steps 1–8 are built and tested against the real models (see the
> README). Step 9, real play and tuning, is next. Pre-fetch was left out: the gatekeeper is fast
> enough without it (see `TODO.md`).

Scope is from `BUILD_SPEC.md`: one Traveller narrative campaign, chat UI → DM model, a vault with one
campaign folder, a router/archivist that keeps the wiki and chooses context, and resume that
always keeps the current scene live. Not in scope: rules modules, story arc, consequence setting,
Docker, a multi-provider config UI, STT/TTS.

## Shape of the system

Two processes share one vault:

- **Adventure:** the player and the DM model. The DM sees a small, focused context and can look
  things up in the wiki with read-only tools.
- **Router/archivist:** keeps an LLM-wiki (gazetteer, notes, scene logs, campaign brief) up to
  date, and decides before each turn which parts of the past the DM needs. It replaces sending the
  whole chat.

## Stack

- **Backend:** Python 3.12, FastAPI, managed with `uv`. Models are called through the `openai` SDK
  pointed at any OpenAI-compatible `base_url`: llama.cpp, LiteLLM, OpenAI, Anthropic's compatible
  endpoint and so on. The app is a standalone container that runs no models itself.
- **Frontend:** plain HTML/JS served by FastAPI, mobile-friendly, no build step. The DM reply
  streams over SSE. Qwen's thinking output is shown collapsed.
- **Storage:** plain files in the vault (markdown, YAML and JSONL). No database. The vault path
  comes from config so it can be a mounted volume later.
- **Auth:** none; LAN only for milestone 1.
- **Wiki tools:** OpenAI function-calling tools that the app runs itself. They're written so an MCP
  wrapper can be added later.

## Config (`.env`)

Three model slots, each with a base URL, API key and model name:

| Slot       | Job                                                 | Default                |
|------------|-----------------------------------------------------|------------------------|
| `DM`       | the adventure                                       | `qwen3.8-27b`          |
| `ROUTER`   | per-turn gatekeeper + scene tracking (small, fast)  | Qwen3 8B; `DM` until it runs |
| `ARCHIVER` | compaction: scene logs, wiki notes, campaign brief  | same as `DM`           |

Other settings:
- `VAULT_PATH`
- `LIVE_TAIL_PCT`: live-tail budget as a percentage of the DM's context window. The window size is
  read from `/v1/models` (llama.cpp `meta.n_ctx`) or `/slots`, with a manual override.
- `ROUTER_THRESHOLD`: the confidence a scene transition needs before it counts.
- `GATEKEEPER_ENABLED`: turns off the router call before each turn, e.g. for paid APIs.
- `IDLE_COMPACT_HOURS`: how long the chat must be idle before compaction runs.

## Vault layout (per campaign)

```
vault/campaigns/<slug>/
  campaign.yaml        # name, system, setup options
  brief.md             # ALWAYS in context: premise, current situation, active threads, party/ship
  transcript.jsonl     # raw log, ground truth: {id, ts, role, content, reasoning, scene_id, supersedes}
  state.yaml           # scene list: open | closed_provisional | compacted, plus router verdicts
  gazetteer.yaml       # [{name, aliases, type, path, summary}]: the fast index
  timeline.md          # chronological entries linking [[scenes]] / [[locations]] / [[npcs]]
  scenes/NNN-<slug>.md # per-scene log: summary + [[links]] + transcript range
  locations/<slug>.md  # "Current state" summary on top + one dated entry per visit
  npcs/<slug>.md       # same pattern
```

Notes use `[[wikilinks]]` so the vault opens cleanly in Obsidian.

## Prompt layout (for prompt caching)

```
[system prompt + DM tool definitions]   stable
[brief.md]                              changes only after compaction
[live tail: current scene onward]       grows, never rewritten mid-scene
[gatekeeper context for this turn]      changes each turn, kept at the end
[player message]
```

The part that changes every turn goes last, so llama.cpp reuses its prompt cache for the rest and
processes only the new text. Measured on the inference box (2026-09-24): a cold 15k-token prompt
takes 21.5 s (prefill ~714 tok/s); the same prompt with a new message on the end takes 0.6 s.
Reprocessing a ~200k-token chat takes about 4.7 min, which accounts for the current waits.

If the current scene alone goes over `LIVE_TAIL_PCT`, the oldest part of that scene is folded
into an "earlier in this scene" summary. The transcript stays complete.

## How a turn works

1. The player's message is appended to `transcript.jsonl`.
2. **Gatekeeper:** a cheap name/alias match against the gazetteer, then (if enabled) a router call
   that reads the player message, the last exchange, the gazetteer and the scene index. It returns
   the notes and scenes to include, which covers implicit references ("the planet where we lost the
   cargo"). Anything already pre-fetched is reused.
3. The DM reply streams to the UI. The DM may call the wiki tools during the reply: `lookup`,
   `read_note`, `read_scene`. The reply goes into the transcript, with the reasoning stored
   separately and never sent back to the model.
4. **After the reply,** in the background while the player reads:
   - **Scene tracking:** returns `{transition, confidence, reason, new_location, entities}`. At or
     above the threshold, a boundary is recorded as `closed_provisional`.
   - **Pre-fetch:** the likely-relevant notes for the next turn.

   Nothing is compacted during play.

**Regenerating or editing** the last message writes a new entry with `supersedes` pointing at the
old one. The context uses only the latest version.

## Compaction (automatic, no "End session")

Runs in the background once the chat has been idle for `IDLE_COMPACT_HOURS`, or on load if that
time has passed. The player can stop anywhere, even mid-sentence, and resume exactly there.

1. **Audit** the most recent provisional boundary using the messages that came after it. If it
   looks wrong, undo it.
2. For each confirmed closed scene other than the current one, the archiver model:
   - writes the scene note
   - updates the location and NPC notes (a new dated entry plus a regenerated "Current state")
   - updates `gazetteer.yaml` with distinctive aliases only
   - adds a timeline entry
   - rewrites `brief.md`
   - marks the scene `compacted`
3. Leaves the current scene untouched.

Compaction only files content elsewhere; the transcript is never edited, so any bad call can be
rebuilt from it.

## Build order

1. Scaffold the project, config and LLM client. Smoke test against the llama.cpp endpoint. (Tool
   calling was checked by hand on 2026-09-24 and works.)
2. Vault and transcript modules, plus a plain chat loop in the UI (streamed, thinking collapsed,
   regenerate/edit).
3. Prompt assembly: stable prefix, brief, live tail, and the live-tail budget.
4. Wiki tools for the DM.
5. Scene tracking: prompt with few-shot examples, JSON parsing, threshold, background task.
6. Compaction: audit, scene notes, wiki notes, gazetteer, timeline, brief. Idle trigger.
7. Gatekeeper: alias match, then the router call before each turn, then pre-fetch.
8. Open WebUI importer: load the existing Traveller chat, then run scene tracking and compaction
   over its history to seed the vault.
9. Play it for real and tune the prompts and threshold. Measure the gatekeeper's latency.

Tests use a fake OpenAI-compatible responder so the logic can be checked without GPUs.

## Still needed from Billy

- The Open WebUI JSON export of the Traveller chat (needed at step 8).
