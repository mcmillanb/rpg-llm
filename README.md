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
- **When you've been idle** (6 hours by default, and not at the campaign for a few minutes;
  it pauses if you start playing): finished scenes are filed into the wiki as
  scene notes, location and NPC notes, a timeline and an updated brief. The current scene is
  never touched. The raw transcript is never edited, so the wiki can always be rebuilt.

Each campaign also has:

- **Table settings,** chosen when you create it: consequences (low-stakes / normal / brutal)
  and dice (rolled by the app and shown in the story / **you roll dice on screen**: the GM sets
  up the moment and asks for a roll, you click the dice, they tumble across the screen, and the
  GM narrates what your roll achieved / you roll your own physical dice / no dice). A 🎲 dice
  tray by the message box rolls any dice, just for fun or sent to the GM as your move. Auto dice
  are real random rolls the GM asks for through a tool; it narrates from the result and can't
  invent one. **Rewinds** (editing your last move or re-rolling the GM's reply after seeing it)
  are off by default; you can always take a message back while the GM is still replying (Esc).
- **Rules mode with a system pack (Daggerheart, and D&D 5e with the 2024 rules):** choose
  "Rules" first when creating a campaign (the system list then shows the games that support it)
  and you get the real character sheet (traits, Evasion, thresholds, Hit
  Points, Stress, Hope and Armor Slots as counters, Experiences, domain cards), a session 0
  where the GM walks you through character creation from the official steps and options, and a
  rules summary plus the official rules text for the GM to look up (`rules_lookup`) instead of
  guessing. Daggerheart's Duality Dice show the Hope and Fear dice and roll "with Hope", "with
  Fear" or critical. Packs live in `src/rpg_llm/packs/`; the rules text is built from the free
  System Reference Documents by `scripts/build_srd.py` (Daggerheart SRD 2.0 under the Darrington
  Press Community Gaming License; D&D SRD 5.2 under CC-BY-4.0; see each pack's
  REFERENCE-LICENSE.md). For D&D, session 0 rolls ability scores on screen (4d6, drop the
  lowest), hands the GM each class's, species' and background's rules text as it's chosen, and
  checks the numbers (scores against the rolls, Hit Points, attack and spell bonuses); options
  from the player's own Player's Handbook are allowed, with a note to confirm them.
- **A character sheet** (money, loans, gear, injuries, skills, assets, companions, debts). New
  campaigns start with one written from the premise; the router updates it after every reply;
  you can edit it. The GM sees it every turn and keeps to it. The router reports each payment
  and the app adds them up; money never goes below zero (borrowing goes under Loans), and an
  update that would take it negative gets a second look, then a flag and a note to the GM. Taking a turn back also undoes
  what that turn did to the sheet.
- **Reminder panels** for forgetful humans: a rail of icons beside the chat (in the footer on a
  phone) opens **Character**, **Gear** (money, kit, ship and property), **People** (everyone
  you've met: pronouns, role, looks, where they are, how they feel about you, a few notes),
  **Places** (where you are, where you've been, what's there), and a **Journal** (promises,
  debts and deadlines, open threads, the story so far). A dot marks a panel that changed since
  you last looked; on a wide screen you can pin one open beside the chat. They only show what
  your character knows, never the story arc.
- **A cast list:** after each reply the router records the named people in it. Pronouns, looks
  and role are written once and kept (only you can change them), and the people in play ride in
  the GM's notes every turn, so a character can't quietly change sex or job. If recent replies
  did slip, the notes tell the GM to correct it. An older campaign reads its whole story for
  people the first time (a few minutes, in the background).
- **A hidden story arc:** GM-only notes (the real conflict, factions, possible beats and
  climaxes, secrets) written at setup and revised between sessions when play goes somewhere
  else. It's guidance, not rails. The play page can't show it; admin can, behind a spoiler
  warning.
- **Suggestions when you create it:** the game-system picker lists what the GM model says it
  can run, and a premise suggester writes openings (or builds on your idea) that you can flip
  back and forth between.

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

## Accounts and running it on the internet

Out of the box there are no accounts: anyone who can reach the server can play every game and
change the settings. That's fine on your own network. Before exposing it, open **admin →
Accounts → Turn on logins** and create your admin account. From then on:

- every page and API call needs a login (a signed session cookie, 30 days); the login page is
  `/login`, and after 5 wrong passwords for a username or address it waits 15 minutes;
- each person sees and manages only their own campaigns, the admin included; someone else's
  campaign is "not found", even by its address. The games made before logins were turned on
  become the admin's;
- the admin also manages the server (model connections, image generation, tuning) and the
  accounts (add someone with a password you give them, reset a password, remove an account:
  its games stay in the vault but nobody can open them). Players get "My games" in the admin
  page: their campaigns' settings, wiki tools, story arc, the Open WebUI import, and their
  password.

Behind a reverse proxy (Traefik, Caddy, nginx), terminate HTTPS at the proxy and forward to
the app's port; the session cookie is marked secure when the proxy says the request came over
HTTPS (`X-Forwarded-Proto`). Accounts, hashed passwords and the cookie-signing secret live in
`vault/config.yaml` (owner-only permissions), so back up the vault.

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
  campaign.yaml      name, owner, system, premise, GM instructions, consequences, dice, rewinds
  brief.md           always in the GM's context
  arc.md             hidden story arc (GM only); earlier versions in arc-history/
  character.yaml     character sheet; every version in character.history.jsonl
  cast.yaml          people met (pronouns, role, looks, notes); recent versions in cast.history.jsonl
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
uv run python evals/soak.py --base http://localhost:8702   # long automated playtest (test vault!)
```
