"""Import a chat exported from Open WebUI (Settings > Chats > Export, or a single chat's
"Export chat (.json)"). Follows the branch that was on screen (history.currentId), so
regenerated-away replies are left out."""

import json
import re
from pathlib import Path

from rpg_llm.vault import Campaign, Vault

_REASONING = re.compile(r'<details type="reasoning"[^>]*>.*?<summary>.*?</summary>(.*?)</details>',
                        re.S)
_THINK = re.compile(r"<think>(.*?)</think>", re.S)


def split_reasoning(content: str) -> tuple[str, str]:
    reasoning = []
    for rx in (_REASONING, _THINK):
        reasoning += [m.strip() for m in rx.findall(content)]
        content = rx.sub("", content)
    # Open WebUI quotes reasoning lines with "> "
    reasoning = [re.sub(r"^> ?", "", r, flags=re.M) for r in reasoning]
    return content.strip(), "\n\n".join(reasoning).strip()


def load_chats(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    chats = data if isinstance(data, list) else [data]
    return [c.get("chat", c) | {"_title": c.get("title") or c.get("chat", {}).get("title")}
            for c in chats]


def branch(chat: dict) -> list[dict]:
    """The visible message branch, oldest first."""
    history = chat.get("history") or {}
    msgs = history.get("messages") or {}
    cur = history.get("currentId")
    if msgs and cur:
        out = []
        while cur:
            m = msgs[cur]
            out.append(m)
            cur = m.get("parentId")
        return out[::-1]
    return chat.get("messages") or []


def import_chat(vault: Vault, chat: dict, name: str | None = None, system: str = "",
                premise: str = "") -> Campaign:
    title = name or chat.get("_title") or chat.get("title") or "Imported campaign"
    params = chat.get("params") or {}
    campaign = vault.create(title, premise=premise, system=system,
                            dm_instructions=(params.get("system") or "").strip())
    entries = []
    for m in branch(chat):
        content, reasoning = split_reasoning(m.get("content") or "")
        if m.get("role") not in ("user", "assistant") or not content:
            continue
        entries.append({"id": len(entries) + 1, "ts": float(m.get("timestamp") or 0),
                        "role": m["role"], "content": content,
                        **({"reasoning": reasoning} if reasoning else {}),
                        "imported_from": m.get("id")})
    # written in one go (keeping the original timestamps) into the brand-new transcript
    campaign.path("transcript.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
    return campaign
