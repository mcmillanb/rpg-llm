# Milestone 1 Plan: Minimal Working Loop

Scope is from `BUILD_SPEC.md`: one Traveller narrative campaign, chat UI → DM model, a vault with one
campaign folder, a router that detects scene transitions and writes to the wiki, and resume that
always keeps the current scene live. Not in scope: rules modules, story arc, consequence setting,
Docker, a multi-provider config UI, STT/TTS.

## Stack

- **Backend:** Python 3.12, FastAPI, managed with `uv`. Models are called through the `openai` SDK
  pointed at any OpenAI-compatible `base_url` (LiteLLM for now).
- **Frontend:** one static HTML/JS page served by FastAPI. No build step. The DM reply streams
  over SSE.
- **Storage:** plain files in the vault (markdown, YAML and JSONL). No database. The vault path
  comes from config so it can be a mounted volume later.
- **Config:** `.env` holds two model slots (`DM_*` and `ROUTER_*`), each with a base URL, API key
  and model name, plus `VAULT_PATH`.

## Repo layout

```
rpg-llm/
  pyproject.toml
  .env.example
  src/rpg_llm/
    app.py            # FastAPI routes: chat (SSE), campaign load, end-session
    config.py         # env -> settings (two model slots, vault path)
    llm.py            # thin OpenAI-compatible client wrapper for both slots
    vault.py          # read/write campaign files; the only module that touches disk
    transcript.py     # append-only JSONL log
    context.py        # assembles the DM prompt from the stores
    gazetteer.py      # name/alias matching -> wiki note injection
    router.py         # transition detection (structured JSON + confidence)
    compactor.py      # between-session batch: audit, summarise, write wiki, trim
    importers/openwebui.py
    prompts/          # DM system prompt, router prompt with few-shot examples, summariser prompts
    static/index.html
  tests/
  docs/
```

## Vault layout (per campaign)

```
vault/campaigns/<slug>/
  campaign.yaml        # name, system, setup options
  transcript.jsonl     # raw log, ground truth: {id, ts, role, content, scene_id}
  state.yaml           # scene list: open | closed_provisional | compacted, plus router verdicts
  story-so-far.md      # narrative summary, one section appended per compacted scene
  timeline.md          # chronological entries linking to [[scene]] / [[location]] notes
  gazetteer.yaml       # [{name, aliases, type, path}], small and loaded every turn
  scenes/NNN-<slug>.md
  locations/<slug>.md
  npcs/<slug>.md
```

Notes use `[[wikilinks]]` so the vault opens cleanly in Obsidian.

## How a turn works

1. The player's message is appended to `transcript.jsonl`.
2. **Gazetteer check** (plain string matching, no LLM): if the message names a known location or
   NPC by name or alias, that note is added to context for this turn.
3. **Context assembly:** DM system prompt, then `story-so-far.md`, then any injected notes, then
   the live tail (every message from the first uncompacted scene onward).
4. The DM reply streams to the UI and is appended to the transcript.
5. **Router pass** (a background task, so the next turn doesn't wait on it): looks at the last few
   exchanges and returns `{transition, confidence, reason, new_location, entities}`. At or above
   the threshold, a scene boundary is recorded as `closed_provisional`. **Nothing is compacted
   during play.**

## Between sessions (compaction)

The compactor is triggered by an "End session" button, or on load when the last message is more
than N hours old. It:

1. **Audits** the most recent provisional boundary using the messages that came after it. If the
   player backtracked or still refers to the old scene, the boundary is undone.
2. For each confirmed closed scene other than the current one:
   - writes a scene note
   - appends a section to `story-so-far.md`
   - creates or updates the location and NPC notes and `gazetteer.yaml`
   - adds a timeline entry
   - marks the scene `compacted`
3. Leaves the current scene untouched, so resume picks up exactly where play stopped.

Compaction only files content elsewhere; the transcript is never edited, so any bad call can be
rebuilt from it.

## Build order

1. Scaffold the project, config and LLM client, with a smoke test against LiteLLM.
2. Vault and transcript modules, plus a plain chat loop in the UI (streamed, full history).
3. Context assembly using the live tail and story-so-far.
4. Router: prompt with few-shot examples, JSON parsing, threshold, background task.
5. Compactor: audit, summarise, then write the wiki, gazetteer and timeline.
6. Gazetteer injection when a location or NPC comes back.
7. Open WebUI importer: load the existing Traveller chat, then run the router and compactor over
   its history to seed the vault.
8. Play it for real and tune the prompts and threshold.

Tests use a fake OpenAI-compatible responder so the router and compactor logic can be checked
without GPUs.

## Still needed from Billy

- The Open WebUI JSON export of the Traveller chat (Settings → Chats → Export, or the per-chat
  export).
- The LiteLLM base URL and the model names for the DM and router slots.
