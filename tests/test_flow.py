import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from rpg_llm import compactor, context, router, wiki
from rpg_llm.app import Runtime, create_app
from rpg_llm.config import AppConfig, Env, Settings, Tuning
from rpg_llm.vault import Scene, State, Vault


class FakeLLM:
    """Scripted stand-in for LLMClient. `json_replies` are consumed in order per schema."""

    def __init__(self, json_replies=None, chat_reply="", stream_rounds=None):
        self.json_replies = list(json_replies or [])
        self.chat_reply = chat_reply
        self.stream_rounds = list(stream_rounds or [])
        self.calls = []

    async def json(self, messages, schema, max_tokens=1500):
        self.calls.append(("json", messages))
        return self.json_replies.pop(0)

    async def chat(self, messages, **kw):
        self.calls.append(("chat", messages))
        return {"content": self.chat_reply}

    async def stream(self, messages, **kw):
        self.calls.append(("stream", messages, kw))
        for d in self.stream_rounds.pop(0):
            yield d

    async def context_window(self):
        return 32768


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path)


def play(c, *pairs):
    for user, gm in pairs:
        c.append({"role": "user", "content": user})
        c.append({"role": "assistant", "content": gm})


# ---- vault ------------------------------------------------------------------

def test_superseded_messages_are_hidden_but_kept(vault):
    c = vault.create("Test")
    play(c, ("hi", "hello"))
    c.append({"role": "assistant", "content": "hello again", "supersedes": [2]})
    assert [m["content"] for m in c.messages()] == ["hi", "hello again"]
    assert len(c.transcript()) == 3


# ---- context ----------------------------------------------------------------

def test_prompt_starts_at_first_unfiled_scene_and_notes_ride_last_message(vault):
    c = vault.create("Test", premise="A premise.")
    play(c, ("a", "A"), ("b", "B"), ("c", "C"))
    c.append({"role": "user", "content": "d"})
    state = State(scenes=[Scene(1, 1, "compacted"), Scene(2, 3, "closed_provisional"),
                          Scene(3, 5)])
    msgs = context.build(c, state, c.messages(), gm_notes="Pell owes us.")
    assert msgs[0]["role"] == "system" and "A premise." in msgs[0]["content"]
    assert [m["content"] for m in msgs[1:-1]] == ["b", "B", "c", "C"]
    assert msgs[-1]["content"].startswith("[GM NOTES]\nPell owes us.")
    assert msgs[-1]["content"].endswith("d")


# ---- scene tracking ---------------------------------------------------------

def verdict(transition, confidence, new_location=None):
    return {"transition": transition, "confidence": confidence, "reason": "r",
            "new_location": new_location, "scene_title": "Old scene" if transition else None,
            "location_now": new_location or "bar"}


async def test_track_opens_scene_only_above_threshold(vault):
    c = vault.create("Test")
    play(c, ("a", "A"), ("we leave", "You arrive at the ship."))
    low = {**verdict(True, 0.5, "ship"), "location_now": "bar"}
    await router.track(c, FakeLLM([low]), threshold=0.7)
    assert len(c.load_state().scenes) == 1
    assert c.load_state().current.location == "bar"  # location_now fills an unknown location

    await router.track(c, FakeLLM([verdict(True, 0.9, "ship")]), threshold=0.7)
    s = c.load_state().scenes
    assert [(x.status, x.start) for x in s] == [("closed_provisional", 1), ("open", 3)]
    assert s[0].title == "Old scene"


async def test_regenerate_undoes_scene_opened_by_that_exchange(vault):
    c = vault.create("Test")
    play(c, ("a", "A"), ("we leave", "You arrive."))
    await router.track(c, FakeLLM([verdict(True, 0.9, "ship")]), threshold=0.7)
    router.undo_scenes_from(c, 3)
    s = c.load_state().scenes
    assert len(s) == 1 and s[0].status == "open"


async def test_audit_merges_a_wrong_boundary(vault):
    c = vault.create("Test")
    play(c, ("a", "A"), ("b", "B"), ("c", "C"))
    c.save_state(State(scenes=[Scene(1, 1, "closed_provisional"), Scene(2, 3, "closed_provisional"),
                               Scene(3, 5)]))
    llm = FakeLLM([{"keep": False, "confidence": 0.9, "reason": "went straight back"},
                   {"keep": True, "confidence": 0.9, "reason": "fine"}])
    await router.audit(c, llm)
    s = c.load_state().scenes
    assert [(x.id, x.start, x.status) for x in s] == [(1, 1, "closed_provisional"), (2, 5, "open")]
    assert s[1].audited


# ---- compaction -------------------------------------------------------------

def archive_reply(title, location, npcs):
    ent = lambda n: {"name": n, "aliases": [], "visit": f"{n} did things.",
                     "current_state": f"{n} now."}
    return {"title": title, "summary": f"{title} happened.", "timeline": f"{title}.",
            "location": ent(location), "npcs": [ent(n) for n in npcs], "others": []}


async def test_compaction_files_closed_scenes_and_keeps_current_live(vault):
    c = vault.create("Test", premise="P")
    play(c, ("a", "A"), ("b", "B"), ("c", "C"))
    c.save_state(State(scenes=[Scene(1, 1, "closed_provisional", audited=True),
                               Scene(2, 3, "closed_provisional", audited=True),
                               Scene(3, 5, audited=True)]))
    archiver = FakeLLM([archive_reply("At the bar", "Ruie Bar", ["Brandt"]),
                        archive_reply("On the ship", "Wandering Star", ["Oskar Brandt"])],
                       chat_reply="# Test\n\n## Situation\n\nOn the ship.")
    report = await compactor.compact(c, FakeLLM(), archiver, asyncio.Lock())

    s = c.load_state().scenes
    assert [x.status for x in s] == ["compacted", "compacted", "open"]
    assert c.load_state().live_start() == 5
    names = {e["name"]: e for e in c.gazetteer()}
    # "Brandt" then "Oskar Brandt" merged into one note under the fuller name
    assert set(names) == {"Ruie Bar", "Oskar Brandt", "Wandering Star"}
    assert names["Oskar Brandt"]["aliases"] == ["Brandt"]
    note = c.read(names["Oskar Brandt"]["path"])
    assert note.count("### Scene") == 2 and "## Current state\n\nOskar Brandt now." in note
    assert "[[scenes/001-at-the-bar]]" in note
    assert "On the ship." in c.brief
    assert "Scene 2: On the ship" in c.read("timeline.md")
    assert len(report["filed"]) == 2


def test_places_are_not_loosely_merged():
    gz = [{"name": "Efate", "aliases": [], "type": "location", "path": "locations/efate.md"}]
    assert wiki.find(gz, "Efate Startown", "location") is None


# ---- gatekeeper -------------------------------------------------------------

async def test_gatekeeper_combines_alias_hits_and_router_picks(vault):
    c = vault.create("Test")
    c.save_gazetteer([
        {"name": "Pell", "aliases": ["grey coat"], "type": "npc", "path": "npcs/pell.md"},
        {"name": "Oskar Brandt", "aliases": [], "type": "npc", "path": "npcs/oskar-brandt.md"},
    ])
    c.write("npcs/pell.md", "# Pell\n\n## Current state\n\nA broker.\n")
    c.write("npcs/oskar-brandt.md", "# Oskar Brandt\n\n## Current state\n\nLoan shark.\n")
    llm = FakeLLM([{"notes": ["npcs/oskar-brandt.md", "npcs/made-up.md"], "scenes": [99],
                    "reason": "r"}])
    notes, info = await router.gatekeep(c, llm, "I look for the Grey Coat and the loan shark",
                                        "", timeout=5)
    assert info["injected"] == ["npcs/pell.md", "npcs/oskar-brandt.md"]
    assert "A broker." in notes and "Loan shark." in notes


async def test_gatekeeper_survives_router_failure(vault):
    c = vault.create("Test")
    c.save_gazetteer([{"name": "Pell", "aliases": [], "type": "npc", "path": "npcs/pell.md"}])

    class Broken(FakeLLM):
        async def json(self, *a, **k):
            raise RuntimeError("down")

    notes, info = await router.gatekeep(c, Broken(), "Where's Pell?", "", timeout=5)
    assert info["injected"] == ["npcs/pell.md"] and "skipped" in info["router"]


# ---- API --------------------------------------------------------------------

def sse_events(resp):
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def test_chat_turn_streams_runs_tools_and_logs(tmp_path):
    settings = Settings(Env(vault_path=tmp_path), AppConfig(tuning=Tuning(gatekeeper_enabled=False)))
    dm = FakeLLM(stream_rounds=[
        [{"reasoning": "hmm"}, {"tool_calls": [{"id": "1", "name": "lookup",
                                                "arguments": '{"name": "Pell"}'}]}],
        [{"content": "Pell is "}, {"content": "a broker."}],
    ])
    rt = Runtime(settings, dm=dm, router_llm=FakeLLM([verdict(False, 0.9)]), archiver=FakeLLM())
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "T"}).json()["slug"]
        c = rt.vault.get(slug)
        c.save_gazetteer([{"name": "Pell", "aliases": [], "type": "npc", "path": "npcs/pell.md",
                           "summary": "A broker."}])
        c.write("npcs/pell.md", "# Pell\n\n## Current state\n\nA broker.\n")
        r = client.post(f"/api/campaigns/{slug}/chat", json={"content": "Who is Pell?"})
        events = sse_events(r)
        assert [e["type"] for e in events if e["type"] in ("tool", "content", "done")] == \
            ["tool", "content", "content", "done"]
        tool_msg = dm.calls[1][1][-1]
        assert tool_msg["role"] == "tool" and "A broker." in tool_msg["content"]

        msgs = client.get(f"/api/campaigns/{slug}").json()["messages"]
        assert [m["content"] for m in msgs] == ["Who is Pell?", "Pell is a broker."]
        assert msgs[1]["reasoning"] == "hmm" and msgs[1]["tools"][0]["tool"] == "lookup"


def test_regenerate_replaces_last_reply(tmp_path):
    settings = Settings(Env(vault_path=tmp_path), AppConfig(tuning=Tuning(gatekeeper_enabled=False)))
    dm = FakeLLM(stream_rounds=[[{"content": "first"}], [{"content": "second"}]])
    rt = Runtime(settings, dm=dm, router_llm=FakeLLM([verdict(False, 0.9)] * 2),
                 archiver=FakeLLM())
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "T", "allow_rewind": True}).json()["slug"]
        client.post(f"/api/campaigns/{slug}/chat", json={"content": "go"})
        client.post(f"/api/campaigns/{slug}/regenerate")
        msgs = client.get(f"/api/campaigns/{slug}").json()["messages"]
        assert [m["content"] for m in msgs] == ["go", "second"]
        # the regenerated prompt must not contain the discarded reply
        assert all(m["content"] != "first" for m in dm.calls[1][1])


# ---- import -----------------------------------------------------------------

def test_openwebui_import_follows_visible_branch_and_splits_reasoning(vault, tmp_path):
    from rpg_llm.importers import openwebui

    export = [{"title": "Traveller", "chat": {"params": {"system": "Be gritty."}, "history": {
        "currentId": "a2b",
        "messages": {
            "u1": {"id": "u1", "parentId": None, "role": "user", "content": "Hi", "timestamp": 100},
            "a1": {"id": "a1", "parentId": "u1", "role": "assistant", "content": "old reply",
                   "timestamp": 101},
            "a2b": {"id": "a2b", "parentId": "u1", "role": "assistant", "timestamp": 102,
                    "content": '<details type="reasoning" done="true"><summary>Thought</summary>\n'
                               '> pondering\n</details>\nWelcome aboard.'},
        }}}}]
    p = tmp_path / "export.json"
    p.write_text(json.dumps(export))
    chat = openwebui.load_chats(p)[0]
    c = openwebui.import_chat(vault, chat, system="Traveller")
    msgs = c.messages()
    assert [m["content"] for m in msgs] == ["Hi", "Welcome aboard."]
    assert msgs[1]["reasoning"] == "pondering" and msgs[1]["ts"] == 102
    assert c.meta["name"] == "Traveller" and c.meta["dm_instructions"] == "Be gritty."


async def test_backfill_segments_history_and_resumes(vault):
    c = vault.create("Test")
    play(c, ("a", "A"), ("go", "At the ship."), ("b", "B"))
    # the opening exchange only sets the location, so it can't open a scene even if asked
    llm = FakeLLM([verdict(True, 0.9, "bar"), verdict(True, 0.9, "ship"), verdict(False, 0.9)])
    assert await router.backfill(c, llm, 0.7) == 1
    s = c.load_state()
    assert [(x.start, x.status, x.location) for x in s.scenes] == \
        [(1, "closed_provisional", "bar"), (3, "open", "ship")]
    assert s.tracked_until == 6
    assert await router.backfill(c, FakeLLM([]), 0.7) == 0  # nothing left to do


# ---- fold -------------------------------------------------------------------

async def test_fold_condenses_oldest_live_messages_and_survives_scene_change(vault):
    c = vault.create("Test")
    play(c, *[(f"u{i} " + "x" * 300, f"g{i} " + "y" * 300) for i in range(6)])
    archiver = FakeLLM(chat_reply="Condensed story.")
    await compactor.fold_current_scene(c, archiver, keep_tokens=250, lock=asyncio.Lock())
    state = c.load_state()
    assert state.fold["summary"] == "Condensed story."
    kept = context.live_tail(state, c.messages())
    assert 2 <= len(kept) < 12 and kept[-1]["content"].startswith("g5")
    assert "Condensed story." in context.build(c, state, c.messages())[0]["content"]

    # a scene change must not bring the condensed messages back
    c.append({"role": "user", "content": "we leave"})
    c.append({"role": "assistant", "content": "At the ship."})
    await router.track(c, FakeLLM([verdict(True, 0.9, "ship")]), threshold=0.7)
    assert c.load_state().fold is not None


def test_dialogue_is_not_movement_evidence():
    reply = ('Serevane stands. "Forty minutes. The *Kestrel.* Not the bay. The *hull.*" '
             'She walks out.')
    assert router.quote_is_dialogue("Forty minutes. The *Kestrel.* Not the bay. The *hull.*", reply)
    assert not router.quote_is_dialogue("She walks out.", reply)
    assert not router.quote_is_dialogue("none", reply)
    assert not router.quote_is_dialogue("something paraphrased", reply)


def test_same_place_by_name_containment():
    assert router.same_place("Maren's Gutter", "Back room of Maren's Gutter")
    assert router.same_place("The Wandering Star", "Wandering Star cargo bay")
    assert not router.same_place("Efate system", "Efate startown freight brokers' hall")
    assert not router.same_place(None, "Regina")


def test_edit_last_message_replaces_exchange(tmp_path):
    settings = Settings(Env(vault_path=tmp_path), AppConfig(tuning=Tuning(gatekeeper_enabled=False)))
    dm = FakeLLM(stream_rounds=[[{"content": "You go left."}], [{"content": "You go right."}]])
    rt = Runtime(settings, dm=dm, router_llm=FakeLLM([verdict(False, 0.9)] * 2),
                 archiver=FakeLLM())
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "T", "allow_rewind": True}).json()["slug"]
        client.post(f"/api/campaigns/{slug}/chat", json={"content": "left"})
        client.post(f"/api/campaigns/{slug}/edit", json={"content": "right"})
        msgs = client.get(f"/api/campaigns/{slug}").json()["messages"]
        assert [m["content"] for m in msgs] == ["right", "You go right."]
        assert [m["content"] for m in dm.calls[1][1][1:]] == ["right"]


def test_bracketed_and_the_prefixed_names_match_existing_entry():
    gz = [{"name": "Quantum Flux Modulator Core (QFMC-994)", "aliases": [], "type": "item",
           "path": "things/qfmc.md"}]
    assert wiki.find(gz, "Quantum Flux Modulator Core", "item") is gz[0]
    assert wiki.find(gz, "the quantum flux modulator core", "item") is gz[0]
    assert wiki.normalise_kind("settlement") == "location"


# ---- admin ------------------------------------------------------------------

def form(**over):
    body = {"servers": [{"id": "a", "name": "A", "base_url": "http://a/v1", "api_key": "secret-key"}],
            "dm": {"server": "a", "model": "big"}, "router": {}, "archiver": {},
            "tuning": {}}
    body.update(over)
    return body


def test_first_run_blocks_play_until_admin_setup(tmp_path):
    rt = Runtime(Settings(Env(vault_path=tmp_path), AppConfig()))
    with TestClient(create_app(rt)) as client:
        assert client.get("/api/status").json()["configured"] is False
        slug = client.post("/api/campaigns", json={"name": "T"}).json()["slug"]
        assert client.post(f"/api/campaigns/{slug}/chat", json={"content": "hi"}).status_code == 503

        cfg = client.put("/api/admin/config", json=form()).json()
        assert cfg["servers"][0]["api_key"].startswith("••••") and cfg["servers"][0]["api_key"].endswith("-key")
        assert client.get("/api/status").json()["configured"] is True
        assert rt.dm is not None and rt.dm.slot.model == "big"
        # saving again with the masked key keeps the real one
        client.put("/api/admin/config", json=form(servers=cfg["servers"]))
        assert rt.settings.app.servers[0].api_key == "secret-key"
        assert (tmp_path / "config.yaml").exists()


def test_admin_password_locks_admin_api(tmp_path):
    rt = Runtime(Settings(Env(vault_path=tmp_path), AppConfig()))
    with TestClient(create_app(rt)) as client:
        client.put("/api/admin/config", json=form(new_password="pw"))  # this browser stays in
        assert client.get("/api/admin/config").status_code == 200
        client.post("/api/admin/logout")
        client.cookies.clear()
        assert client.get("/api/admin/config").status_code == 401
        assert client.post("/api/admin/login", json={"password": "nope"}).status_code == 401
        assert client.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        assert client.get("/api/admin/config").status_code == 200


def test_delete_moves_campaign_to_trash(tmp_path):
    rt = Runtime(Settings(Env(vault_path=tmp_path), AppConfig()))
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "Old game"}).json()["slug"]
        assert client.delete(f"/api/admin/campaigns/{slug}").status_code == 200
        assert client.get("/api/campaigns").json() == []
        assert len(list((tmp_path / "trash").iterdir())) == 1


# ---- background filing vs play ------------------------------------------------

async def test_background_filing_waits_for_the_player(tmp_path, monkeypatch):
    import rpg_llm.app as app_mod

    rt = Runtime(Settings(Env(vault_path=tmp_path), AppConfig(tuning=Tuning(idle_compact_hours=0))),
                 dm=FakeLLM(), router_llm=FakeLLM(), archiver=FakeLLM())
    c = rt.vault.create("T")
    play(c, ("a", "A"), ("b", "B"))
    c.save_state(State(scenes=[Scene(1, 1, "closed_provisional", audited=True),
                               Scene(2, 3, audited=True)]))

    rt.touch(c.slug)  # the player just opened the campaign
    rt.maybe_compact_idle(c)
    assert not rt.st(c.slug)["compacting"]  # doesn't start while they're there

    calls = []

    async def fake_compact(campaign, router_llm, archiver, lock, pause=None):
        await pause()
        calls.append("filed")
        return {"filed": [], "seconds": 0}

    monkeypatch.setattr(app_mod.compactor, "compact", fake_compact)
    monkeypatch.setattr(app_mod, "QUIET_SECONDS", 0.05)
    monkeypatch.setattr(app_mod, "QUIET_POLL", 0.01)
    rt.turns[c.slug] = 1  # a turn is running: filing must hold off
    task = asyncio.create_task(rt.run_compact(c, background=True))
    await asyncio.sleep(0.1)
    assert calls == []
    rt.turns[c.slug] = 0
    rt.seen[c.slug] = 0
    await asyncio.wait_for(task, 10)
    assert calls == ["filed"]


async def test_gatekeeper_skips_what_is_already_in_play(vault):
    c = vault.create("Test")
    c.save_gazetteer([
        {"name": "Efate", "aliases": [], "type": "location", "path": "locations/efate.md"},
        {"name": "Wandering Star", "aliases": [], "type": "ship", "path": "things/ws.md"},
        {"name": "Pell", "aliases": [], "type": "npc", "path": "npcs/pell.md"},
    ])
    c.save_state(State(scenes=[Scene(1, 1, location="Efate startown")]))
    llm = FakeLLM([{"notes": ["locations/efate.md", "things/ws.md", "npcs/pell.md"],
                    "scenes": [], "reason": "r"}])
    _, info = await router.gatekeep(c, llm, "I haggle, then ask about Pell",
                                    "Back aboard the Wandering Star, Dex waits.", timeout=5)
    assert info["injected"] == ["npcs/pell.md"]


def test_unsend_withdraws_a_message_the_gm_has_not_answered(tmp_path):
    settings = Settings(Env(vault_path=tmp_path), AppConfig(tuning=Tuning(gatekeeper_enabled=False)))
    dm = FakeLLM(stream_rounds=[[{"content": "One."}]])
    rt = Runtime(settings, dm=dm, router_llm=FakeLLM([verdict(False, 0.9)]), archiver=FakeLLM())
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "T"}).json()["slug"]
        client.post(f"/api/campaigns/{slug}/chat", json={"content": "first"})
        # as if Esc was pressed mid-reply: the message is in, no reply saved yet
        rt.vault.get(slug).append({"role": "user", "content": "oops, half a thou"})
        r = client.post(f"/api/campaigns/{slug}/unsend").json()
        assert r["content"] == "oops, half a thou"
        msgs = client.get(f"/api/campaigns/{slug}").json()["messages"]
        assert [m["content"] for m in msgs] == ["first", "One."]
        assert msgs[-1]["stats"]["total"] >= 0 and "first_word" in msgs[-1]["stats"]
        # once the GM has replied it's too late (rewinds are off by default)
        assert client.post(f"/api/campaigns/{slug}/unsend").status_code == 409


def test_rewinds_are_off_by_default_but_failed_turns_can_be_retried(tmp_path):
    settings = Settings(Env(vault_path=tmp_path), AppConfig(tuning=Tuning(gatekeeper_enabled=False)))
    dm = FakeLLM(stream_rounds=[[{"content": "It works."}], [{"content": "Retried."}]])
    rt = Runtime(settings, dm=dm, router_llm=FakeLLM([verdict(False, 0.9)] * 2), archiver=FakeLLM())
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "T"}).json()["slug"]
        assert client.get(f"/api/campaigns/{slug}").json()["can_rewind"] is False
        client.post(f"/api/campaigns/{slug}/chat", json={"content": "go"})
        assert client.post(f"/api/campaigns/{slug}/regenerate").status_code == 403
        assert client.post(f"/api/campaigns/{slug}/edit", json={"content": "x"}).status_code == 403
        # a turn that got no reply (error, or stopped) can be retried
        rt.vault.get(slug).append({"role": "user", "content": "next"})
        assert client.post(f"/api/campaigns/{slug}/regenerate").status_code == 200
        msgs = client.get(f"/api/campaigns/{slug}").json()["messages"]
        assert [m["content"] for m in msgs][-2:] == ["next", "Retried."]


def test_delete_from_play_page_moves_to_trash(tmp_path):
    rt = Runtime(Settings(Env(vault_path=tmp_path), AppConfig()))
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "Old"}).json()["slug"]
        assert client.delete(f"/api/campaigns/{slug}").status_code == 200
        assert client.get("/api/campaigns").json() == []
        assert (tmp_path / "trash").exists()

# ---- table rules & dice -----------------------------------------------------

def test_dice_roll_and_description():
    from rpg_llm import dice
    for _ in range(200):
        r = dice.roll("2D6+1", "spot the tail", 8)
        assert 3 <= r["total"] <= 13 and len(r["rolls"]) == 2 and r["success"] == (r["total"] >= 8)
    r = dice.roll("d%", "library use", 45, "at_most")
    assert 1 <= r["total"] <= 100 and r["dice"] == "1D100"
    assert "Needed 45 or less" in dice.describe(r)
    with pytest.raises(ValueError):
        dice.roll("two dice")
    with pytest.raises(ValueError):
        dice.roll("500d6")


def test_table_settings_reach_the_prompt_and_old_campaigns_get_no_dice(vault):
    c = vault.create("Old")
    assert context.table(c.meta) == {"consequences": "normal", "dice": "none"}
    c.save_meta({**c.meta, "consequences": "brutal", "dice": "auto"})
    sp = context.system_prompt(c, c.load_state())
    assert "BRUTAL" in sp and "roll_dice" in sp


def test_auto_dice_turn_rolls_real_dice_and_records_them(tmp_path):
    settings = Settings(Env(vault_path=tmp_path), AppConfig(tuning=Tuning(gatekeeper_enabled=False)))
    dm = FakeLLM(stream_rounds=[
        [{"tool_calls": [{"id": "1", "name": "roll_dice",
                          "arguments": '{"dice": "2D6", "reason": "sneak", "target": 8}'}]}],
        [{"content": "You slip past."}],
    ])
    rt = Runtime(settings, dm=dm, router_llm=FakeLLM([verdict(False, 0.9)]), archiver=FakeLLM())
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "T", "dice": "auto"}).json()["slug"]
        events = sse_events(client.post(f"/api/campaigns/{slug}/chat", json={"content": "I sneak"}))
        roll = next(e for e in events if e["type"] == "roll")
        assert roll["reason"] == "sneak" and 2 <= roll["total"] <= 12
        assert any(t["function"]["name"] == "roll_dice" for t in dm.calls[0][2]["tools"])
        assert "Narrate from this result" in dm.calls[1][1][-1]["content"]
        msg = client.get(f"/api/campaigns/{slug}").json()["messages"][-1]
        assert msg["rolls"][0]["total"] == roll["total"]


# ---- character sheet --------------------------------------------------------

def sheet_reply(changed=True, **fields):
    from rpg_llm import sheet
    s = {**sheet.blank(), "name": "Mara", "money": "Cr 500", **fields}
    return {"changes": ["spent Cr 100"] if changed else [], "changed": changed, "sheet": s}


async def test_sheet_updates_rewinds_and_reaches_the_gm(vault):
    from rpg_llm import sheet
    c = vault.create("T")
    play(c, ("I buy a medkit", "The trader hands it over."))
    assert await sheet.update(c, FakeLLM([sheet_reply(gear=["medkit"])]), "sys")
    assert sheet.load(c)["gear"] == ["medkit"]
    # nothing changed: no new version
    play(c, ("I look around", "Quiet."))
    assert await sheet.update(c, FakeLLM([sheet_reply(changed=False, gear=["medkit"])]), "sys") is None
    assert len(sheet.history(c)) == 1
    play(c, ("I sell the medkit", "Sold."))
    await sheet.update(c, FakeLLM([sheet_reply(money="Cr 600")]), "sys")
    assert sheet.load(c)["gear"] == [] and sheet.load(c)["money"] == "Cr 600"
    # taking back the last exchange restores the sheet from before it
    assert sheet.rewind(c, 5)
    assert sheet.load(c)["gear"] == ["medkit"] and sheet.load(c)["money"] == "Cr 500"
    block = sheet.notes_block(sheet.load(c))
    assert "Money: Cr 500" in block and "Gear: medkit" in block


def test_player_can_edit_the_sheet_and_it_rides_in_gm_notes(tmp_path):
    settings = Settings(Env(vault_path=tmp_path), AppConfig(tuning=Tuning(gatekeeper_enabled=False)))
    dm = FakeLLM(stream_rounds=[[{"content": "Noted."}]])
    rt = Runtime(settings, dm=dm, router_llm=FakeLLM(), archiver=FakeLLM())
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "T"}).json()["slug"]
        r = client.put(f"/api/campaigns/{slug}/character",
                       json={"name": "Mara", "money": "Cr 50", "gear": ["knife", ""], "bogus": 1})
        assert r.json()["sheet"]["gear"] == ["knife"] and "bogus" not in r.json()["sheet"]
        client.post(f"/api/campaigns/{slug}/chat", json={"content": "hi"})
        last = dm.calls[0][1][-1]["content"]
        assert last.startswith("[GM NOTES]") and "Money: Cr 50" in last
        h = client.get(f"/api/campaigns/{slug}/character").json()["history"]
        assert h[0]["what"] == "edited by the player"


# ---- story arc --------------------------------------------------------------

def test_arc_is_in_the_gm_prompt_but_never_served_to_the_player(tmp_path):
    rt = Runtime(Settings(Env(vault_path=tmp_path), AppConfig()))
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "T"}).json()["slug"]
        c = rt.vault.get(slug)
        c.write("arc.md", "## Core conflict\nThe broker is the villain.\n\n<!-- stamp -->\n")
        assert "The broker is the villain." in context.system_prompt(c, c.load_state())
        assert "<!--" not in context.system_prompt(c, c.load_state())
        for path in ("arc.md", "./arc.md", "arc-history/001.md"):
            assert client.get(f"/api/campaigns/{slug}/file", params={"path": path}).status_code == 403
        assert "villain" in client.get(f"/api/admin/campaigns/{slug}/arc").json()["text"]


async def test_arc_revision_keeps_history_and_refuses_truncated_answers(vault):
    from rpg_llm import arc
    c = vault.create("T")
    c.write("arc.md", "## Core conflict\n" + "x" * 400)
    c.write("scenes/001-a.md", "# Scene 1\n\nThey fled.\n")
    filed = [{"note": "scenes/001-a.md"}]
    r = await arc.revise(c, FakeLLM([{"diverged": True, "reason": "fled", "arc": "## Core conflict\n" + "y" * 400}]), filed)
    assert r["diverged"] and "y" * 50 in c.read("arc.md") and "revised: fled" in c.read("arc.md")
    assert "x" * 50 in c.read("arc-history/001.md")
    r = await arc.revise(c, FakeLLM([{"diverged": True, "reason": "?", "arc": "short"}]), filed)
    assert "skipped" in r["reason"] and "y" * 50 in c.read("arc.md")


def test_turn_stats_count_context_once_across_tool_rounds(tmp_path):
    settings = Settings(Env(vault_path=tmp_path), AppConfig(tuning=Tuning(gatekeeper_enabled=False)))
    usage = lambda p, c: {"usage": {"prompt_tokens": p, "cached_tokens": c, "prompt_ms": 100,
                                    "completion_tokens": 50}}
    dm = FakeLLM(stream_rounds=[
        [{"tool_calls": [{"id": "1", "name": "roll_dice", "arguments": '{"dice": "2D6", "reason": "x"}'}]},
         usage(5000, 4000)],
        [{"content": "Done."}, usage(5100, 5000)],
    ])
    rt = Runtime(settings, dm=dm, router_llm=FakeLLM(), archiver=FakeLLM())
    with TestClient(create_app(rt)) as client:
        slug = client.post("/api/campaigns", json={"name": "T", "dice": "auto"}).json()["slug"]
        client.post(f"/api/campaigns/{slug}/chat", json={"content": "go"})
        st = client.get(f"/api/campaigns/{slug}").json()["messages"][-1]["stats"]
        assert (st["prompt_tokens"], st["cached_tokens"], st["rounds"]) == (5000, 4000, 2)
        assert st["prompt_ms"] == 200 and st["completion_tokens"] == 100
