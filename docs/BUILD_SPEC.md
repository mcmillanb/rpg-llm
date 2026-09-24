# AI RPG Interface - Build Spec

Sep 24, 2026 · @Billy McMillan

## Overview

A custom web interface to replace Open WebUI for solo AI-DM'd RPG adventures. Currently runs via Open WebUI chat using Qwen 3.6 27B (may move to Qwen 3.8) as the DM model, hosted on a Pop!\_OS inference box with dual V100 GPUs.

Problem being solved: Open WebUI's context compaction kicks in on every message once context gets large, slowing everything down. The fix is a purpose-built context management layer, not just a bigger context window.

Intended for others too, not just personal use — not everyone has this homelab setup, so the design must support configurable backend models rather than being hardcoded.

## Core architecture

**Context layers.** Four distinct stores, not one blob:

1. Raw transcript — full append-only chat log, kept as ground truth, rarely read directly.
2. Narrative summary — human-readable, novel-style "story so far", built incrementally per scene rather than re-summarized from scratch each time.
3. Location/NPC wiki — Obsidian-style markdown vault, one file per location or NPC.
4. Gazetteer index — one small file per campaign listing every known location/NPC with name, aliases, and a pointer to its full wiki file. This is what makes lookups fast: the router checks this tiny index first rather than searching the whole wiki. Since these adventures are narrative teleportation (no map, no proximity), lookup is simple name/alias matching, not semantic/vector search.

A separate flat timeline file holds chronological entries pointing back into the wiki and narrative summary.

**Router model.** A background model (not tiny — needs real comprehension) that:

- Periodically batch-processes the live chat to judge when a scene/location has been left, then summarizes it into the wiki/timeline and trims the live context to just the current leg.
- Fronts the main DM model: on a prompt mentioning travel to a place, checks the gazetteer for a name match and injects that location's history back in if revisited.
- Should run in parallel with the main DM model (not strictly sequential) to avoid added latency — dual V100 box gives room for this.
- Model choice: lean toward a dense model like Qwen3 8B rather than a mixture-of-experts model (e.g. Qwen3 30B-A3B). MoE's main advantage is broad parametric knowledge at low compute cost, but this task is about instruction-following and structural judgment over provided text, not knowledge recall — so a well-tuned dense 8B is the sensible starting point. Only escalate to MoE if the 8B fumbles subtler transition judgments.
- Reliability approach: have the router return structured output (a confidence score plus short reason) rather than an open-ended verdict, with a threshold before acting. Include few-shot examples in the prompt of what counts as a transition (new named location, time skip, deliberate decision to leave) versus what doesn't (conversation/combat continuing in the same place).

**Wiki organization.** One Obsidian vault, organized by campaign, not by rules system. Each campaign gets its own folder containing its scenes, locations, NPCs, and gazetteer index — this prevents name clashes/bleed between separate campaigns (e.g. two different Traveller campaigns, or Traveller vs Starforged).

**Build approach.** Prototype the whole skeleton end-to-end together rather than building the router in isolation — the router can't really be tested without the rest of the pipeline in place.

## Rules system ingestion and modules

**Narrative vs rules-based play (sliding scale).** The interface should support a dial from fully narrative freeform (current usage — no rules module needed at all, the DM model runs from general knowledge of the setting) up to strict rules-based play (full character generation and rulebook adherence). Start with one system (Traveller, since rulebooks are owned) to get the principle right before generalizing.

**Ingestion process.** One-time offline conversion, not done at query time:

1. Run owned rulebook PDFs through a document parser (e.g. Docling or Marker) to extract structured markdown, since tables and stat blocks get mangled by naive text extraction.
2. Hand-correct the output, especially tables.
3. Organize by topic: character creation, skills, combat, equipment, etc. — same pattern as the location wiki, but for rules.
4. Build a fast lookup index (table of contents) for the rules content — rules queries need precision ("what's the modifier for zero-G combat"), so this should be direct index lookup, not fuzzy semantic/vector search.

**Folder structure.** One reference folder per rules system (shared across all campaigns using that system), separate from the per-campaign folders holding actual play data. A Traveller reference folder can sit underneath multiple different Traveller campaigns.

**Modules: import/export.** Package a rules system as a compressed folder bundle plus a manifest (system name, version) — reusable and versionable, e.g. GitHub-hosted with point releases (1.0, 1.1) for corrections/errata. Same underlying mechanism handles campaign backup/restore.

Plan to offer ready-made downloadable modules for open source and public-domain/out-of-copyright rules systems. For copyrighted rulebooks (e.g. Traveller), each user converts their own owned PDF rather than redistributing converted files — the format and tooling can be shared even when the content can't.

**Character state.** Distinct from the narrative wiki: mechanical state (HP, skills, inventory, XP, conditions) is structured, frequently-changing data, not prose — likely wants its own format (e.g. YAML front matter on a character file) rather than being treated as a wiki note.

## Session continuity and audit review

**Pause/resume problem.** Real play happens in irregular sessions (e.g. play one day, return a week later) that don't align with in-story scene boundaries. The player needs to resume exactly mid-scene, picking up from literally the last prompt, the same way Open WebUI's chat history works today.

**Resolution.** Do not interrupt play with a live "should I compact now?" question — that breaks immersion. Instead:

- The current/most-recent scene always stays fully live and untouched in context, regardless of how much time has passed since it was played.
- Only scenes the router is confident are fully closed get compacted into the wiki.
- The actual compaction/summarization runs as a background batch job between sessions, not during play.

This means resuming a session a week later reloads the same live tail exactly as left, mid-scene included, while everything already finished stays safely folded into the wiki.

**Audit/review step.** Treat a scene-transition call as provisional rather than final. At the start of the next session/run, the router re-checks the previous transition with the benefit of hindsight (e.g. did the player immediately backtrack, does new dialogue reference something that should still be live) and either confirms it or pulls that content back into the active context. This is cheap because nothing is destroyed by compaction, only filed elsewhere, so undoing a bad call is low-cost.

## Campaign setup options and hidden story arc

**Setup-time sliders/toggles**, baked into the DM model's system prompt so they don't need repeating mid-session:

- Rules strictness: narrative freeform through to strict rules-based (see above).
- Dice handling: auto-rolled by the system (for on-the-go narrative play) versus manual/virtual dice rolls (for hands-on rules-based play).
- Consequences: brutal / normal / low-stakes. LLM DMs default to pleasant, protective storytelling unless told otherwise. Brutal explicitly allows failure, death, and permanent loss when earned by the story; normal allows real setbacks without being punishing; low-stakes keeps things cinematic and safe.

**Hidden story arc.** At campaign setup, generate a long-term story arc/outline (key plot beats, major NPCs, possible climaxes) and store it as its own GM-only file in the campaign folder, alongside the wiki but never shown to the player or folded into the player-facing summary. It rides along in the main DM model's context as guidance, not a rigid script. If the player diverges significantly, the arc gets revised (similar mechanism to scene-transition detection) rather than being ignored or rewritten silently.

## Deployment and configuration

**Distribution.** Docker container, following the same pattern as Open WebUI.

**Model backend configuration.** Build against a standard OpenAI-compatible chat completions API rather than hard-wiring to llama.cpp, so any provider can be plugged in (local llama.cpp/LiteLLM, Claude, ChatGPT, etc.). Config screen lets each user set endpoint URL, API key, and model name independently for two slots: the main DM model and the summarizer/router model. For this setup specifically, both slots would point at the existing LiteLLM gateway fronting the homelab's tiered models.

**Vault storage location.** The Obsidian markdown vault must not live inside the Docker container itself — config needs a way to set its location on the host filesystem via a mounted volume.

**Speech-to-text / text-to-speech.** Later optional layer, not an initial feature. Whisper/Faster Whisper is the likely STT choice (already proven for D&D session recap work on existing hardware). Local TTS options like Piper or Kokoro are worth considering when this layer gets built.

## Build order recommendation

Build the skeleton end-to-end first, rather than perfecting any one piece in isolation — the router model's behavior can't really be judged without the full pipeline (raw log, wiki, gazetteer, DM model) in place to react to.

Suggested first milestone: a minimal working loop on the existing Traveller narrative campaign, no rules module, no multi-user config, no STT/TTS. That means: chat UI talking to the main DM model, a basic Obsidian vault with one campaign folder, a router model doing scene-transition detection and wiki writes with the structured-confidence-score approach, and the always-keep-current-scene-live resume behavior. Rules modules, story arc generation, consequence dial, Docker packaging, and multi-provider config layer on top once that loop feels right in actual play.
