"""Campaign folders on disk. The only module that knows the vault layout."""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


def estimate_tokens(text: str) -> int:
    return len(text) // 3 + 1  # conservative; Qwen averages ~3.5-4 chars/token on English


@dataclass
class Scene:
    id: int
    start: int  # first transcript id in the scene
    status: str = "open"  # open | closed_provisional | compacted
    location: str | None = None
    title: str | None = None
    confidence: float | None = None  # router verdict that opened this scene
    reason: str | None = None
    audited: bool | None = None  # boundary re-checked with hindsight before compaction
    note: str | None = None  # vault-relative path once compacted


@dataclass
class State:
    scenes: list[Scene] = field(default_factory=list)
    fold: dict | None = None  # {"until": msg id, "summary": str} for an oversized current scene

    @property
    def current(self) -> Scene:
        return self.scenes[-1]

    def live_start(self) -> int:
        """Transcript id where the live tail begins: the first scene not yet compacted."""
        for s in self.scenes:
            if s.status != "compacted":
                return s.start
        return self.scenes[-1].start


class Campaign:
    def __init__(self, root: Path):
        self.root = root
        self.slug = root.name

    # ---- plain files ------------------------------------------------------

    def path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError(f"path escapes campaign folder: {rel}")
        return p

    def read(self, rel: str, default: str = "") -> str:
        p = self.path(rel)
        return p.read_text() if p.exists() else default

    def write(self, rel: str, text: str) -> None:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(p)

    def _yaml(self, rel: str, default):
        text = self.read(rel)
        return yaml.safe_load(text) if text.strip() else default

    def _write_yaml(self, rel: str, data) -> None:
        self.write(rel, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    # ---- structured files -------------------------------------------------

    @property
    def meta(self) -> dict:
        return self._yaml("campaign.yaml", {})

    def save_meta(self, meta: dict) -> None:
        self._write_yaml("campaign.yaml", meta)

    def load_state(self) -> State:
        raw = self._yaml("state.yaml", {})
        scenes = [Scene(**s) for s in raw.get("scenes", [])] or [Scene(id=1, start=1)]
        return State(scenes=scenes, fold=raw.get("fold"))

    def save_state(self, state: State) -> None:
        self._write_yaml("state.yaml", {
            "scenes": [{k: v for k, v in vars(s).items() if v is not None} for s in state.scenes],
            "fold": state.fold,
        })

    def gazetteer(self) -> list[dict]:
        return self._yaml("gazetteer.yaml", [])

    def save_gazetteer(self, entries: list[dict]) -> None:
        self._write_yaml("gazetteer.yaml", entries)

    @property
    def brief(self) -> str:
        return self.read("brief.md")

    # ---- transcript -------------------------------------------------------

    def transcript(self) -> list[dict]:
        p = self.path("transcript.jsonl")
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def append(self, entry: dict) -> dict:
        """Append to the raw log. Never rewrites earlier lines."""
        entries = self.transcript()
        entry = {"id": (entries[-1]["id"] + 1) if entries else 1, "ts": time.time(), **entry}
        with self.path("transcript.jsonl").open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def messages(self) -> list[dict]:
        """Active messages: the transcript minus anything superseded by a regenerate/edit."""
        entries = self.transcript()
        dead = {i for e in entries for i in e.get("supersedes", [])}
        return [e for e in entries if e["id"] not in dead and e["role"] in ("user", "assistant")]

    def last_activity(self) -> float | None:
        entries = self.transcript()
        return entries[-1]["ts"] if entries else None


class Vault:
    def __init__(self, root: Path):
        self.root = root
        (root / "campaigns").mkdir(parents=True, exist_ok=True)

    def campaigns(self) -> list[Campaign]:
        return sorted(
            (Campaign(p) for p in (self.root / "campaigns").iterdir() if (p / "campaign.yaml").exists()),
            key=lambda c: -(c.last_activity() or 0),
        )

    def get(self, slug: str) -> Campaign:
        c = Campaign(self.root / "campaigns" / slugify(slug))
        if not (c.root / "campaign.yaml").exists():
            raise KeyError(slug)
        return c

    def create(self, name: str, premise: str = "", system: str = "", dm_instructions: str = "") -> Campaign:
        slug = slugify(name)
        root = self.root / "campaigns" / slug
        n = 2
        while root.exists():
            root = self.root / "campaigns" / f"{slug}-{n}"
            n += 1
        c = Campaign(root)
        c.save_meta({"name": name, "system": system, "premise": premise,
                     "dm_instructions": dm_instructions, "created": time.time()})
        c.write("brief.md", f"# {name}\n\n## Premise\n\n{premise.strip() or '(not set)'}\n")
        c.write("timeline.md", f"# Timeline: {name}\n\n")
        c.save_gazetteer([])
        c.save_state(State(scenes=[Scene(id=1, start=1)]))
        return c
