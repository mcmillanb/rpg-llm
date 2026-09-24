# rpg-llm

A self-hosted web interface for solo RPG adventures run by an AI game master (GM). Instead of
sending the whole chat to the model every turn, a second "router/archivist" model keeps a
campaign wiki (an Obsidian-style markdown vault) and gives the GM only what it needs:

- **Always in context:** a short campaign brief, plus the current stretch of play, kept word for
  word. You can stop anywhere, even mid-fight, and resume exactly where you left off.
- **Each turn:** the router checks your message against the wiki index and adds any relevant
  notes, including indirect references ("the loan shark back home"). The GM can also look things
  up itself with wiki tools.
- **After each reply,** in the background: the router decides whether the scene has changed.
- **When you've been idle** (6 hours by default): finished scenes are filed into the wiki as
  scene notes, location and NPC notes, a timeline and an updated brief. The current scene is
  never touched. The raw transcript is never edited, so the wiki can always be rebuilt.

The prompt is ordered so the model server can reuse its prompt cache: typically 70–95% of each
turn's prompt comes from cache, so replies don't slow down as the campaign grows. Each GM reply
shows a ⚡ chip with the context size and cache hit rate.

Design: [`docs/BUILD_SPEC.md`](docs/BUILD_SPEC.md), [`docs/MILESTONE_1_PLAN.md`](docs/MILESTONE_1_PLAN.md).

## Models

Any OpenAI-compatible chat completions endpoint works (llama.cpp, LM Studio, LiteLLM, vLLM,
OpenAI, …). There are three model settings; the router and archiver fall back to the DM's if left
unset:

| Setting    | Does                                               | Tested with                        |
|------------|----------------------------------------------------|------------------------------------|
| `DM_*`     | runs the game                                      | `qwen3.8-27b` on llama.cpp         |
| `ROUTER_*` | per-turn wiki lookup + scene tracking (fast)       | `qwen/qwen3-vl-8b` on LM Studio    |
| `ARCHIVER_*` | writing the wiki between sessions (quality)      | same as DM                         |

The DM server needs tool calling (llama.cpp: `--jinja`). The router and archiver need JSON
schema output (`response_format`), which llama.cpp and LM Studio both support.

## Run

```bash
cp .env.example .env        # then set DM_BASE_URL / DM_MODEL (and ROUTER_* if you have one)
uv run python scripts/smoke.py   # checks each model: context window, a reply, a tool call
uv run rpg-llm              # http://localhost:8700
```

Or with Docker (the vault stays on the host in `./vault`, owned by your user):

```bash
docker compose up -d --build
```

## Settings (`.env`)

| Variable | Default | |
|---|---|---|
| `DM_BASE_URL`, `DM_MODEL`, `DM_API_KEY` | | the GM model |
| `ROUTER_…`, `ARCHIVER_…` | DM's values | same fields per model |
| `*_CONTEXT_WINDOW` | detected | override if the server doesn't report it |
| `DM_THINKING` | true | false skips Qwen's reasoning: replies start ~5–10 s sooner, possibly less considered |
| `VAULT_PATH` | `./vault` | campaign folders live in `VAULT_PATH/campaigns/` |
| `LIVE_TAIL_PCT` | 50 | share of the GM's context window the live tail may use before the oldest part of the current scene is condensed |
| `ROUTER_THRESHOLD` | 0.7 | confidence needed to call a scene change |
| `GATEKEEPER_ENABLED` | true | router call before each turn (turn off for paid APIs; name matching still runs) |
| `GATEKEEPER_TIMEOUT` | 30 | seconds before the turn goes ahead without it |
| `IDLE_COMPACT_HOURS` | 6 | idle time before closed scenes are filed |

## Importing an Open WebUI chat

Export from Open WebUI (a single chat's "Export chat (.json)", or Settings → Chats → Export),
stop the server, then:

```bash
uv run python scripts/import_openwebui.py export.json \
    --system "Traveller (Mongoose 2e), Third Imperium" --premise-file premise.md
```

The importer follows the branch that was on screen, keeps Qwen's thinking separate, splits the
history into scenes with the router (about 5 seconds per exchange), then files everything but the
last scene. A premise file (your character, crew, ship, situation) helps the archiver get names
right. If it's interrupted, pick up again with `--resume <campaign-slug>`.

## The vault

```
vault/campaigns/<campaign>/
  campaign.yaml      name, system, premise, extra GM instructions
  brief.md           always in the GM's context
  transcript.jsonl   everything ever said (append-only; regenerate/edit supersede, never delete)
  state.yaml         scene boundaries and status
  gazetteer.yaml     wiki index: name, aliases, type, path, one-line summary
  timeline.md
  scenes/ locations/ npcs/ things/    notes with [[wikilinks]]
```

Open `vault/` (or a campaign folder) in Obsidian to browse it. To rebuild a campaign's wiki
from its transcript after changing prompts or models: `uv run python scripts/rebuild_wiki.py
<campaign>` (server stopped).

## Development

```bash
uv run pytest               # unit tests, fake models, no GPU needed
uv run python evals/track.py      # router prompt evals against the real models
uv run python evals/gatekeep.py
```
