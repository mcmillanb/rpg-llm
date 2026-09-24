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

## Run

```bash
uv run rpg-llm              # then open http://localhost:8700
```

or with Docker (the vault stays on the host in `./vault`, owned by your user):

```bash
docker compose up -d --build
```

There is no config file to write. The first visit goes to the **admin page** (`/admin`, also
the ⚙ button):

1. **Add a connection** for each model server: any OpenAI-compatible endpoint (llama.cpp,
   LM Studio, Ollama, vLLM, LiteLLM, OpenAI…). It lists the server's models straight away. Add
   one per server, e.g. one llama.cpp instance per GPU on different ports. Tick "loads one model
   at a time" for LM Studio-style servers; it's ticked for you when LM Studio is detected.
2. **Pick a model for each job** from the combined list:

   | Job | Does | Suggested |
   |---|---|---|
   | Game master | runs the game; needs tool calling (llama.cpp: `--jinja`) | your best model; ~64k context per slot |
   | Router | per-turn recall + scene tracking; needs JSON-schema output | small and fast, e.g. Qwen3-8B; 16–32k context |
   | Archiver | writes the wiki between sessions | "same as the game master" |

   **Test** checks each one: context window, a reply, tool calling or JSON output.
3. **Save**, then play.

Settings live in `<vault>/config.yaml` (owner-only permissions, since it can hold API keys), so
they travel with the vault and survive container rebuilds. Only three optional environment
variables exist: `VAULT_PATH` (default `./vault`), `HOST` (`0.0.0.0`), `PORT` (`8700`).

The admin page also has the tuning values, an optional admin password, campaign management
(edit, file closed scenes now, rebuild the wiki, delete to trash) and the Open WebUI import.

| Tuning | Default | |
|---|---|---|
| Live context budget | 50% | share of the GM's context window the current stretch of play may use before its oldest part is condensed |
| Scene-change threshold | 0.7 | router confidence needed to call a scene change |
| Idle hours before filing | 6 | finished scenes are filed after this long without play |
| Recall before every turn | on | the router's per-turn check (name matching still runs when off) |
| Recall timeout | 30 s | the turn goes ahead without it after this |

## Importing an Open WebUI chat

In the admin page, choose the export file (a chat's "Export chat (.json)", or Settings → Chats →
Export), pick the chat, give it a premise (your character, crew, ship, situation; it helps the
archiver get names right) and import. It follows the branch that was on screen, splits the
history into scenes with the router (about 5 seconds per exchange), then files everything but
the last scene. It runs in the background with progress shown on the page.

The same is available from the command line (`scripts/import_openwebui.py --help`), which can
resume an interrupted import.

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
from its transcript after changing prompts or models, use "Rebuild wiki" in the admin page.

## Development

```bash
uv run pytest               # unit tests, fake models, no GPU needed
uv run python scripts/smoke.py    # checks the configured models from the command line
uv run python evals/track.py      # router prompt evals against the real models
uv run python evals/gatekeep.py
```
