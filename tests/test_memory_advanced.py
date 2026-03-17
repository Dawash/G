"""Tests for the 3-layer advanced memory system.

Covers:
  - WorkingMemory  (memory/working_memory.py)
  - EpisodicMemory (memory/episodic_memory.py)
  - SemanticMemory — KnowledgeGraph + VectorStore (memory/semantic_memory.py)
  - MemoryAPI      (memory/memory_api.py)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# =============================================================================
# WorkingMemory
# =============================================================================

class TestWorkingMemory:
    def _wm(self):
        from memory.working_memory import WorkingMemory
        return WorkingMemory()

    def test_add_and_retrieve_message(self):
        wm = self._wm()
        wm.add_message("user", "hello world")
        msgs = wm.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello world"

    def test_sliding_window_caps_at_max(self):
        wm = self._wm()
        for i in range(30):
            wm.add_message("user", f"message {i} about music")
        msgs = wm.get_messages()
        assert len(msgs) <= wm.MAX_WINDOW

    def test_get_last_n(self):
        wm = self._wm()
        for i in range(10):
            wm.add_message("user", f"msg {i}")
        last = wm.get_last_n(3)
        assert len(last) == 3
        assert last[-1]["content"] == "msg 9"

    def test_topic_detected_from_keywords(self):
        wm = self._wm()
        wm.add_message("user", "open spotify music")
        wm.add_message("user", "play spotify track")
        # topic should resolve to the most common keyword
        assert wm.current_topic != ""

    def test_clear_resets_state(self):
        wm = self._wm()
        wm.add_message("user", "hello")
        wm.clear()
        assert wm.get_messages() == []
        assert wm.current_topic == ""

    def test_active_task_default_idle(self):
        wm = self._wm()
        assert wm.active_task.status == "idle"

    def test_start_task_sets_active(self):
        wm = self._wm()
        wm.start_task("open app", ["find app", "launch app"])
        assert wm.active_task.status == "active"
        assert wm.active_task.goal == "open app"

    def test_complete_step_advances(self):
        wm = self._wm()
        wm.start_task("goal", ["step1", "step2"])
        wm.complete_step({"result": "ok"})
        assert wm.active_task.current_step == 1
        assert "step1" in wm.active_task.completed_steps

    def test_complete_all_steps_marks_completed(self):
        wm = self._wm()
        wm.start_task("g", ["s1"])
        wm.complete_step()
        assert wm.active_task.status == "completed"

    def test_timestamps_included_when_requested(self):
        wm = self._wm()
        wm.add_message("assistant", "hi")
        msgs = wm.get_messages(include_timestamps=True)
        assert "timestamp" in msgs[0]

    def test_thread_safety(self):
        wm = self._wm()
        errors = []

        def _writer(role):
            try:
                for i in range(20):
                    wm.add_message(role, f"concurrent {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_writer, args=(r,)) for r in ["user", "assistant"]]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == []

    def test_window_size_stable_topic(self):
        """Same-topic messages → MAX_WINDOW retained."""
        wm = self._wm()
        for i in range(25):
            wm.add_message("user", "spotify music playlist song track")
        assert len(wm.get_messages()) == wm.MAX_WINDOW

    def test_window_size_topic_change(self):
        """Topic change can shrink window to MIN_WINDOW."""
        wm = self._wm()
        for i in range(25):
            wm.add_message("user", f"completely different topic {i}")
        # window capped at MAX by default; may shrink to MIN on unrelated messages
        assert len(wm.get_messages()) <= wm.MAX_WINDOW


# =============================================================================
# EpisodicMemory
# =============================================================================

class TestEpisodicMemory:
    def _ep(self):
        from memory.episodic_memory import EpisodicMemory
        with tempfile.TemporaryDirectory() as d:
            ep = EpisodicMemory(db_path=os.path.join(d, "test_ep.db"))
            yield ep

    @pytest.fixture
    def ep(self):
        from memory.episodic_memory import EpisodicMemory
        with tempfile.TemporaryDirectory() as d:
            yield EpisodicMemory(db_path=os.path.join(d, "test_ep.db"))

    def test_log_episode_returns_id(self, ep):
        eid = ep.log_episode("open chrome", response="Opening Chrome")
        assert eid > 0

    def test_log_episode_stored(self, ep):
        ep.log_episode("find my files", response="Here are your files")
        recent = ep.get_recent(limit=5)
        assert any("find my files" in e.user_input for e in recent)

    def test_search_finds_episode(self, ep):
        ep.log_episode("play jazz music", response="Playing jazz")
        results = ep.search("jazz")
        assert len(results) >= 1
        assert "jazz" in results[0].user_input.lower() or "jazz" in results[0].response.lower()

    def test_search_no_match_returns_empty(self, ep):
        ep.log_episode("open notepad", response="Opening notepad")
        results = ep.search("xyzzyzzyxyz_notexist")
        assert results == []

    def test_get_recent_ordered(self, ep):
        ep.log_episode("first")
        time.sleep(0.01)
        ep.log_episode("second")
        recent = ep.get_recent(limit=2)
        # Most recent first
        assert "second" in recent[0].user_input

    def test_learn_skill_stores_sequence(self, ep):
        sid = ep.learn_skill("open app", [{"tool": "open_app", "args": {"name": "chrome"}}])
        assert sid > 0

    def test_learn_skill_upsert_on_repeat(self, ep):
        ep.learn_skill("do the thing", [{"tool": "t"}])
        sid2 = ep.learn_skill("Do The Thing", [{"tool": "t"}])
        # Should upsert (same normalized goal) not duplicate
        assert sid2 > 0

    def test_find_skill_returns_matching(self, ep):
        ep.learn_skill("play spotify", [{"tool": "play_music"}])
        skill = ep.find_skill("play spotify")
        assert skill is not None
        assert skill.tool_sequence[0]["tool"] == "play_music"

    def test_find_skill_min_reliability(self, ep):
        from memory.episodic_memory import EpisodicMemory
        with tempfile.TemporaryDirectory() as d:
            ep2 = EpisodicMemory(db_path=os.path.join(d, "r.db"))
            sid = ep2.learn_skill("fragile task", [])
            ep2.mark_skill_failure(sid)
            ep2.mark_skill_failure(sid)
            # low reliability — should not find at default 0.7
            result = ep2.find_skill("fragile task", min_reliability=0.7)
            # After 1 success + 2 failures → 1/3 ≈ 0.33 < 0.7
            assert result is None

    def test_mark_skill_success_increments(self, ep):
        sid = ep.learn_skill("a goal", [])
        ep.mark_skill_success(sid)
        skill = ep.find_skill("a goal")
        assert skill is not None
        assert skill.success_count >= 2

    def test_mark_skill_failure_increments(self, ep):
        from memory.episodic_memory import EpisodicMemory
        with tempfile.TemporaryDirectory() as d:
            ep2 = EpisodicMemory(db_path=os.path.join(d, "f.db"))
            sid = ep2.learn_skill("fail task", [])
            ep2.mark_skill_failure(sid)
            skill = ep2.find_skill("fail task", min_reliability=0.0)
            assert skill is not None
            assert skill.fail_count == 1

    def test_log_failure(self, ep):
        ep.log_failure("broken task", "TimeoutError", context="agent mode")
        failures = ep.get_failures_for("broken task")
        assert len(failures) >= 1
        assert failures[0]["error"] == "TimeoutError"

    def test_set_and_get_user_fact(self, ep):
        ep.set_user_fact("favorite_app", "spotify", source="explicit")
        val = ep.get_user_fact("favorite_app")
        assert val == "spotify"

    def test_get_all_user_facts(self, ep):
        ep.set_user_fact("name", "Denis")
        ep.set_user_fact("city", "London")
        facts = ep.get_all_user_facts()
        assert "name" in facts
        assert "city" in facts

    def test_get_stats(self, ep):
        ep.log_episode("test input")
        stats = ep.get_stats()
        assert "episodes" in stats
        assert stats["episodes"] >= 1

    def test_skill_reliability_property(self, ep):
        sid = ep.learn_skill("reliable task", [])
        ep.mark_skill_success(sid)
        skill = ep.find_skill("reliable task")
        assert skill is not None
        assert 0.0 <= skill.reliability <= 1.0

    def test_concurrent_logging(self, ep):
        errors = []

        def _log(i):
            try:
                ep.log_episode(f"concurrent input {i}", response=f"resp {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_log, args=(i,)) for i in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == []
        assert ep.get_stats()["episodes"] == 10


# =============================================================================
# SemanticMemory — KnowledgeGraph
# =============================================================================

class TestKnowledgeGraph:
    @pytest.fixture
    def kg(self):
        from memory.semantic_memory import KnowledgeGraph
        with tempfile.TemporaryDirectory() as d:
            yield KnowledgeGraph(path=os.path.join(d, "test_kg.pkl"))

    def test_add_and_get_entity(self, kg):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        kg.add_entity("spotify", etype="app")
        e = kg.get_entity("spotify")
        assert e is not None
        assert e["etype"] == "app"

    def test_add_relation_creates_both_entities(self, kg):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        kg.add_relation("user", "spotify", "uses")
        assert kg.get_entity("user") is not None
        assert kg.get_entity("spotify") is not None

    def test_get_related_returns_neighbors(self, kg):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        kg.add_relation("music", "spotify", "related_to")
        kg.add_relation("music", "youtube", "related_to")
        related = kg.get_related("music", depth=1)
        assert "spotify" in related
        assert "youtube" in related

    def test_find_path_between_entities(self, kg):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        kg.add_relation("a", "b", "link")
        kg.add_relation("b", "c", "link")
        path = kg.find_path("a", "c")
        assert len(path) >= 2

    def test_find_path_no_path(self, kg):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        kg.add_entity("isolated_x")
        kg.add_entity("isolated_y")
        assert kg.find_path("isolated_x", "isolated_y") == []

    def test_most_connected_returns_sorted(self, kg):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        kg.add_relation("hub", "a", "r")
        kg.add_relation("hub", "b", "r")
        kg.add_relation("leaf", "hub", "r")
        top = kg.most_connected(top_n=3)
        assert any(name == "hub" for name, _ in top)

    def test_stats(self, kg):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        kg.add_entity("test_e")
        s = kg.stats()
        assert s["nodes"] >= 1
        assert s["available"] is True

    def test_stats_when_unavailable(self):
        """When NX unavailable, stats reports gracefully."""
        from memory.semantic_memory import KnowledgeGraph
        with tempfile.TemporaryDirectory() as d:
            kg = KnowledgeGraph(path=os.path.join(d, "nk.pkl"))
            s = kg.stats()
            assert "nodes" in s

    def test_save_and_reload(self):
        from memory.semantic_memory import KnowledgeGraph, _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "kg.pkl")
            kg1 = KnowledgeGraph(path=path)
            kg1.add_entity("persist_me", etype="test")
            kg1.save()
            kg2 = KnowledgeGraph(path=path)
            assert kg2.get_entity("persist_me") is not None

    def test_confidence_reinforced_on_re_add(self, kg):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        kg.add_entity("reinforce_me", confidence=0.5)
        kg.add_entity("reinforce_me", confidence=0.5)
        e = kg.get_entity("reinforce_me")
        # Should increase toward 1.0
        assert e["confidence"] > 0.5


# =============================================================================
# SemanticMemory — VectorStore
# =============================================================================

class TestVectorStore:
    @pytest.fixture
    def vs(self):
        from memory.semantic_memory import VectorStore
        with tempfile.TemporaryDirectory() as d:
            yield VectorStore(path=os.path.join(d, "test_vs.pkl"))

    def test_add_returns_id(self, vs):
        eid = vs.add("play spotify music", source="episode", source_id=1)
        assert eid >= 0

    def test_search_returns_entries(self, vs):
        vs.add("play spotify music", source="episode")
        results = vs.search("spotify", top_k=5)
        assert len(results) >= 1

    def test_search_empty_store(self, vs):
        results = vs.search("anything")
        assert results == []

    def test_search_source_filter(self, vs):
        vs.add("open chrome", source="episode")
        vs.add("open chrome", source="skill")
        results = vs.search("open chrome", source_filter="skill")
        assert all(e.source == "skill" for e, _ in results)

    def test_scores_are_floats(self, vs):
        vs.add("test text for scoring", source="manual")
        results = vs.search("test scoring")
        if results:
            _, score = results[0]
            assert isinstance(score, float)

    def test_save_and_reload(self):
        from memory.semantic_memory import VectorStore
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "vs.pkl")
            vs1 = VectorStore(path=path)
            vs1.add("remember this", source="test")
            vs1.save()
            vs2 = VectorStore(path=path)
            results = vs2.search("remember this")
            assert len(results) >= 1

    def test_stats(self, vs):
        vs.add("entry1", source="test")
        s = vs.stats()
        assert s["entries"] == 1
        assert "faiss_available" in s

    def test_multiple_sources(self, vs):
        vs.add("episode text", source="episode")
        vs.add("skill text", source="skill")
        vs.add("fact text", source="fact")
        assert vs.stats()["entries"] == 3

    def test_text_fallback_no_numpy(self, vs, monkeypatch):
        """Ensure text_fallback runs when numpy embed returns None."""
        import memory.semantic_memory as sm
        monkeypatch.setattr(sm, "_NP_AVAILABLE", False)
        vs.add("fallback test phrase", source="ep")
        results = vs._text_fallback("fallback", top_k=3, source_filter=None)
        assert any("fallback" in e.text for e, _ in results)


# =============================================================================
# MemoryAPI — unified interface
# =============================================================================

class TestMemoryAPI:
    @pytest.fixture
    def api(self):
        from memory.working_memory import WorkingMemory
        from memory.episodic_memory import EpisodicMemory
        from memory.semantic_memory import KnowledgeGraph, VectorStore
        from memory.memory_api import MemoryAPI
        with tempfile.TemporaryDirectory() as d:
            ep = EpisodicMemory(db_path=os.path.join(d, "ep.db"))
            kg = KnowledgeGraph(path=os.path.join(d, "kg.pkl"))
            vs = VectorStore(path=os.path.join(d, "vs.pkl"))
            wm = WorkingMemory()
            yield MemoryAPI(wm=wm, ep=ep, kg=kg, vs=vs)

    def test_add_turn_updates_working_memory(self, api):
        api.add_turn("user", "hello there")
        ctx = api.get_context()
        assert any(m["content"] == "hello there" for m in ctx)

    def test_log_episode_persists(self, api):
        api.log_episode("open browser", response="Opening Chrome", success=True)
        eps = api.recent_episodes(limit=5)
        assert any("open browser" in e.user_input for e in eps)

    def test_log_episode_cross_indexes_vector(self, api):
        api.log_episode("play jazz", response="Playing jazz")
        results = api.recall("jazz")
        assert len(results) >= 1

    def test_learn_skill_and_find(self, api):
        api.learn_skill("open browser", [{"tool": "open_app", "args": {"name": "chrome"}}])
        skill = api.find_skill("open browser")
        assert skill is not None

    def test_find_skill_semantic(self, api):
        api.learn_skill("launch the browser", [{"tool": "open_app"}])
        # Exact match should work via episodic
        skill = api.find_skill("launch the browser")
        assert skill is not None

    def test_log_failure(self, api):
        api.log_failure("broken task", "TimeoutError", lesson="retry with longer timeout")
        failures = api.get_failures_for("broken task")
        assert len(failures) >= 1

    def test_set_and_get_user_fact(self, api):
        api.set_user_fact("preferred_browser", "Firefox", source="explicit")
        assert api.get_user_fact("preferred_browser") == "Firefox"

    def test_user_fact_reflected_in_graph(self, api):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        api.set_user_fact("favorite_music", "jazz")
        entity = api.get_entity("favorite_music")
        assert entity is not None

    def test_learn_and_query_entity(self, api):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        api.learn_entity("chrome", etype="app")
        api.learn_relation("chrome", "google", "made_by")
        related = api.get_related("chrome", depth=1)
        assert "google" in related

    def test_recall_text(self, api):
        api.log_episode("watch youtube videos", response="Opening YouTube")
        texts = api.recall_text("youtube", top_k=3)
        assert isinstance(texts, list)

    def test_learn_from_turn_updates_all_layers(self, api):
        api.learn_from_turn(
            user_input="open Spotify",
            response="Opening Spotify for you",
            tools=["open_app"],
            success=True,
        )
        ctx = api.get_context()
        assert any("open Spotify" in m.get("content", "") for m in ctx)
        eps = api.recent_episodes(limit=3)
        assert any("open Spotify" in e.user_input for e in eps)

    def test_context_for_query_structure(self, api):
        api.log_episode("spotify music", response="Playing")
        ctx = api.context_for_query("spotify")
        assert "working" in ctx
        assert "topic" in ctx
        assert "similar_episodes" in ctx
        assert "semantic_hits" in ctx

    def test_get_stats(self, api):
        api.log_episode("test")
        stats = api.get_stats()
        assert "episodic" in stats
        assert "graph" in stats
        assert "vectors" in stats

    def test_current_topic_property(self, api):
        api.add_turn("user", "spotify music playlist")
        api.add_turn("user", "play spotify track song")
        assert isinstance(api.current_topic, str)

    def test_active_task_property(self, api):
        api.start_task("multi-step goal", ["step1", "step2"])
        assert api.active_task.status == "active"

    def test_clear_working(self, api):
        api.add_turn("user", "some text")
        api.clear_working()
        assert api.get_context() == []

    def test_mark_skill_success_and_failure(self, api):
        sid = api.learn_skill("traceable skill", [{"tool": "t"}])
        api.mark_skill_success(sid)
        api.mark_skill_failure(sid)
        skill = api.find_skill("traceable skill", min_reliability=0.0)
        assert skill is not None

    def test_most_connected_entities(self, api):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        api.learn_relation("hub_node", "a", "r")
        api.learn_relation("hub_node", "b", "r")
        top = api.most_connected_entities(top_n=5)
        assert isinstance(top, list)

    def test_embed_and_store(self, api):
        vid = api.embed_and_store("some text to recall", source="manual")
        assert vid >= 0
        results = api.recall("text recall")
        assert len(results) >= 1

    def test_save_does_not_raise(self, api):
        api.log_episode("saved episode")
        api.save()  # Should not raise

    def test_find_path_between_entities(self, api):
        from memory.semantic_memory import _NX_AVAILABLE
        if not _NX_AVAILABLE:
            pytest.skip("networkx not installed")
        api.learn_relation("source_node", "middle", "link")
        api.learn_relation("middle", "dest_node", "link")
        path = api.find_path("source_node", "dest_node")
        assert len(path) >= 2

    def test_get_all_user_facts(self, api):
        api.set_user_fact("k1", "v1")
        api.set_user_fact("k2", "v2")
        facts = api.get_all_user_facts()
        assert "k1" in facts and "k2" in facts


# =============================================================================
# Integration — singleton imports
# =============================================================================

class TestSingletonImports:
    def test_working_memory_singleton(self):
        from memory.working_memory import working_memory
        assert working_memory is not None

    def test_episodic_singleton(self):
        from memory.episodic_memory import episodic
        assert episodic is not None

    def test_semantic_singletons(self):
        from memory.semantic_memory import knowledge_graph, vector_store
        assert knowledge_graph is not None
        assert vector_store is not None

    def test_memory_api_singleton(self):
        from memory.memory_api import memory
        assert memory is not None

    def test_memory_api_imports_correctly(self):
        from memory.memory_api import MemoryAPI, memory
        assert isinstance(memory, MemoryAPI)

    def test_brain_has_memory_methods(self):
        from brain import Brain
        assert hasattr(Brain, "get_memory_context")
        assert hasattr(Brain, "recall_similar")

    def test_assistant_loop_imports_memory(self):
        path = os.path.join(ROOT, "orchestration", "assistant_loop.py")
        with open(path) as f:
            src = f.read()
        assert "memory.memory_api" in src
        assert "_adv_memory" in src
