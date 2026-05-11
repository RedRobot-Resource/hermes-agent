"""Tests for hermes_cli/goals.py — agent-owned standing-goal checklist."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so SessionDB.state_meta writes don't clobber the real one."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


# ──────────────────────────────────────────────────────────────────────
# GoalState + ChecklistItem round-trip / backcompat
# ──────────────────────────────────────────────────────────────────────


class TestGoalStateRoundTrip:
    def test_old_state_meta_row_loads_without_checklist_fields(self):
        """A goal serialized BEFORE the checklist fields existed must
        round-trip through GoalState.from_json with empty defaults."""
        from hermes_cli.goals import GoalState

        legacy_json = json.dumps({
            "goal": "do the thing",
            "status": "active",
            "turns_used": 3,
            "max_turns": 20,
            "created_at": 1.0,
            "last_turn_at": 2.0,
            "paused_reason": None,
        })
        state = GoalState.from_json(legacy_json)
        assert state.goal == "do the thing"
        assert state.checklist == []
        assert state.pending_challenges == []

    def test_new_state_round_trip(self):
        from hermes_cli.goals import (
            ChecklistItem, GoalState,
            ITEM_COMPLETED, ITEM_PENDING, ADDED_BY_AGENT, ADDED_BY_USER,
        )
        state = GoalState(
            goal="g",
            checklist=[
                ChecklistItem(text="a", status=ITEM_COMPLETED,
                              added_by=ADDED_BY_AGENT, evidence="done"),
                ChecklistItem(text="b", status=ITEM_PENDING,
                              added_by=ADDED_BY_USER),
            ],
            pending_challenges=["challenge nudge text"],
        )
        rt = GoalState.from_json(state.to_json())
        assert len(rt.checklist) == 2
        assert rt.checklist[0].evidence == "done"
        assert rt.checklist[1].added_by == ADDED_BY_USER
        assert rt.pending_challenges == ["challenge nudge text"]

    def test_checklist_counts_and_all_terminal(self):
        from hermes_cli.goals import (
            ChecklistItem, GoalState,
            ITEM_COMPLETED, ITEM_IMPOSSIBLE, ITEM_PENDING,
        )
        state = GoalState(
            goal="g",
            checklist=[
                ChecklistItem(text="a", status=ITEM_COMPLETED),
                ChecklistItem(text="b", status=ITEM_IMPOSSIBLE),
                ChecklistItem(text="c", status=ITEM_PENDING),
            ],
        )
        total, done, imp, pending = state.checklist_counts()
        assert (total, done, imp, pending) == (3, 1, 1, 1)
        assert state.all_terminal() is False

        state.checklist[2].status = ITEM_IMPOSSIBLE
        assert state.all_terminal() is True

    def test_empty_checklist_is_not_all_terminal(self):
        from hermes_cli.goals import GoalState
        assert GoalState(goal="g").all_terminal() is False


# ──────────────────────────────────────────────────────────────────────
# GoalManager basic lifecycle
# ──────────────────────────────────────────────────────────────────────


class TestGoalManagerLifecycle:
    def test_set_and_status(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="s1")
        assert not mgr.is_active()
        mgr.set("ship the feature")
        assert mgr.is_active()
        assert "ship the feature" in mgr.status_line()

    def test_pause_resume_clear(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="s2")
        mgr.set("g")
        mgr.pause(reason="user-paused")
        assert not mgr.is_active()
        assert mgr.has_goal()
        assert "paused" in mgr.status_line().lower()

        mgr.resume()
        assert mgr.is_active()
        assert mgr.state.turns_used == 0  # resume resets budget

        mgr.clear()
        assert not mgr.has_goal()
        assert mgr.state is None

    def test_persistence_across_managers(self, hermes_home):
        from hermes_cli.goals import GoalManager
        m1 = GoalManager(session_id="persist-sid")
        m1.set("a goal")
        m2 = GoalManager(session_id="persist-sid")
        assert m2.is_active()
        assert m2.state.goal == "a goal"


# ──────────────────────────────────────────────────────────────────────
# evaluate_after_turn (no judge call!)
# ──────────────────────────────────────────────────────────────────────


class TestEvaluateAfterTurn:
    def test_inactive_goal_is_noop(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="eval-1")
        d = mgr.evaluate_after_turn()
        assert d["should_continue"] is False
        mgr.set("g")
        mgr.pause()
        d2 = mgr.evaluate_after_turn()
        assert d2["should_continue"] is False

    def test_empty_checklist_continues(self, hermes_home):
        """No checklist yet → loop continues so the agent can decompose."""
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="eval-2", default_max_turns=5)
        mgr.set("g")
        d = mgr.evaluate_after_turn()
        assert d["should_continue"] is True
        # Continuation prompt is the decompose prompt for the empty case.
        assert "propose a checklist" in d["continuation_prompt"]
        assert mgr.state.turns_used == 1

    def test_partial_checklist_continues(self, hermes_home):
        """Some items pending → loop continues with checklist progress."""
        from hermes_cli.goals import (
            GoalManager, ChecklistItem,
            ITEM_PENDING, ITEM_COMPLETED, ADDED_BY_AGENT,
        )
        from hermes_cli import goals
        mgr = GoalManager(session_id="eval-3", default_max_turns=5)
        mgr.set("g")
        mgr.state.checklist = [
            ChecklistItem(text="a", status=ITEM_COMPLETED, added_by=ADDED_BY_AGENT),
            ChecklistItem(text="b", status=ITEM_PENDING, added_by=ADDED_BY_AGENT),
        ]
        goals.save_goal("eval-3", mgr.state)
        d = mgr.evaluate_after_turn()
        assert d["should_continue"] is True
        assert "Checklist progress (1/2 done)" in d["continuation_prompt"]

    def test_all_terminal_marks_done(self, hermes_home):
        from hermes_cli.goals import (
            GoalManager, ChecklistItem,
            ITEM_COMPLETED, ITEM_IMPOSSIBLE, ADDED_BY_AGENT,
        )
        from hermes_cli import goals
        mgr = GoalManager(session_id="eval-4")
        mgr.set("g")
        mgr.state.checklist = [
            ChecklistItem(text="a", status=ITEM_COMPLETED, added_by=ADDED_BY_AGENT),
            ChecklistItem(text="b", status=ITEM_IMPOSSIBLE, added_by=ADDED_BY_AGENT),
        ]
        goals.save_goal("eval-4", mgr.state)
        d = mgr.evaluate_after_turn()
        assert d["should_continue"] is False
        assert d["status"] == "done"
        assert "Goal achieved" in d["message"]

    def test_budget_exhausted_pauses(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="eval-5", default_max_turns=2)
        mgr.set("g")
        d1 = mgr.evaluate_after_turn()
        assert d1["should_continue"] is True
        d2 = mgr.evaluate_after_turn()
        assert d2["should_continue"] is False
        assert d2["status"] == "paused"
        assert "budget" in (mgr.state.paused_reason or "").lower()


# ──────────────────────────────────────────────────────────────────────
# apply_agent_update (called by the goal_checklist tool handler)
# ──────────────────────────────────────────────────────────────────────


class TestApplyAgentUpdate:
    def test_initial_decompose_via_items(self, hermes_home):
        from hermes_cli.goals import GoalManager, ITEM_PENDING, ADDED_BY_AGENT
        mgr = GoalManager(session_id="upd-1")
        mgr.set("build a website")
        result = mgr.apply_agent_update(items=[
            {"text": "homepage exists"},
            {"text": "navigation works"},
            {"text": "deployed"},
        ])
        assert result["ok"] is True
        assert result["summary"]["total"] == 3
        assert result["summary"]["pending"] == 3
        assert all(it.added_by == ADDED_BY_AGENT for it in mgr.state.checklist)
        assert all(it.status == ITEM_PENDING for it in mgr.state.checklist)

    def test_mark_flips_pending_to_terminal(self, hermes_home):
        from hermes_cli.goals import (
            GoalManager, ChecklistItem,
            ITEM_PENDING, ITEM_COMPLETED, ITEM_IMPOSSIBLE, ADDED_BY_AGENT,
        )
        from hermes_cli import goals
        mgr = GoalManager(session_id="upd-2")
        mgr.set("g")
        mgr.state.checklist = [
            ChecklistItem(text="a", status=ITEM_PENDING, added_by=ADDED_BY_AGENT),
            ChecklistItem(text="b", status=ITEM_PENDING, added_by=ADDED_BY_AGENT),
            ChecklistItem(text="c", status=ITEM_PENDING, added_by=ADDED_BY_AGENT),
        ]
        goals.save_goal("upd-2", mgr.state)

        mgr.apply_agent_update(mark=[
            {"index": 1, "status": "completed", "evidence": "ran a"},
            {"index": 3, "status": "impossible", "evidence": "blocked"},
        ])
        assert mgr.state.checklist[0].status == ITEM_COMPLETED
        assert mgr.state.checklist[0].evidence == "ran a"
        assert mgr.state.checklist[1].status == ITEM_PENDING
        assert mgr.state.checklist[2].status == ITEM_IMPOSSIBLE

    def test_stickiness_agent_cannot_regress_terminal(self, hermes_home):
        """Once an item is in a terminal status, the agent cannot flip it
        via apply_agent_update. Only the user can (via /subgoal)."""
        from hermes_cli.goals import (
            GoalManager, ChecklistItem,
            ITEM_COMPLETED, ADDED_BY_AGENT,
        )
        from hermes_cli import goals
        mgr = GoalManager(session_id="upd-stick")
        mgr.set("g")
        mgr.state.checklist = [
            ChecklistItem(
                text="a", status=ITEM_COMPLETED,
                added_by=ADDED_BY_AGENT, evidence="original",
            ),
        ]
        goals.save_goal("upd-stick", mgr.state)

        mgr.apply_agent_update(mark=[
            {"index": 1, "status": "impossible", "evidence": "regression"},
        ])
        assert mgr.state.checklist[0].status == ITEM_COMPLETED
        assert mgr.state.checklist[0].evidence == "original"

    def test_non_terminal_status_in_mark_is_filtered(self, hermes_home):
        """The agent can only mark terminal statuses via the tool; pending
        is filtered (the agent doesn't get to un-finish work)."""
        from hermes_cli.goals import (
            GoalManager, ChecklistItem,
            ITEM_PENDING, ADDED_BY_AGENT,
        )
        from hermes_cli import goals
        mgr = GoalManager(session_id="upd-filter")
        mgr.set("g")
        mgr.state.checklist = [
            ChecklistItem(text="a", status=ITEM_PENDING, added_by=ADDED_BY_AGENT),
        ]
        goals.save_goal("upd-filter", mgr.state)

        mgr.apply_agent_update(mark=[
            {"index": 1, "status": "pending", "evidence": "no-op"},
        ])
        assert mgr.state.checklist[0].status == ITEM_PENDING
        assert mgr.state.checklist[0].evidence is None

    def test_items_appended_when_checklist_already_populated(self, hermes_home):
        from hermes_cli.goals import (
            GoalManager, ChecklistItem,
            ITEM_PENDING, ADDED_BY_AGENT,
        )
        from hermes_cli import goals
        mgr = GoalManager(session_id="upd-append")
        mgr.set("g")
        mgr.state.checklist = [
            ChecklistItem(text="existing", status=ITEM_PENDING, added_by=ADDED_BY_AGENT),
        ]
        goals.save_goal("upd-append", mgr.state)

        mgr.apply_agent_update(items=[
            {"text": "existing"},  # duplicate, dropped
            {"text": "newly discovered"},
        ])
        assert [it.text for it in mgr.state.checklist] == ["existing", "newly discovered"]


# ──────────────────────────────────────────────────────────────────────
# /subgoal user controls
# ──────────────────────────────────────────────────────────────────────


class TestSubgoalUserControls:
    def test_add_subgoal(self, hermes_home):
        from hermes_cli.goals import GoalManager, ITEM_PENDING, ADDED_BY_USER
        mgr = GoalManager(session_id="sub-1")
        mgr.set("g")
        item = mgr.add_subgoal("user added")
        assert item.text == "user added"
        assert item.status == ITEM_PENDING
        assert item.added_by == ADDED_BY_USER

    def test_add_subgoal_requires_active_goal(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="sub-2")
        with pytest.raises(RuntimeError):
            mgr.add_subgoal("x")

    def test_mark_subgoal_user_can_revert_terminal(self, hermes_home):
        """User mark_subgoal bypasses stickiness — only path to revert."""
        from hermes_cli.goals import GoalManager, ITEM_COMPLETED, ITEM_PENDING
        mgr = GoalManager(session_id="sub-3")
        mgr.set("g")
        mgr.add_subgoal("a")
        mgr.mark_subgoal(1, "completed")
        assert mgr.state.checklist[0].status == ITEM_COMPLETED
        mgr.mark_subgoal(1, "pending")
        assert mgr.state.checklist[0].status == ITEM_PENDING

    def test_challenge_flips_terminal_to_pending_and_queues_nudge(self, hermes_home):
        from hermes_cli.goals import GoalManager, ITEM_PENDING
        mgr = GoalManager(session_id="sub-4")
        mgr.set("g")
        mgr.add_subgoal("did the thing")
        mgr.mark_subgoal(1, "completed")
        assert mgr.state.checklist[0].status != ITEM_PENDING
        assert mgr.state.pending_challenges == []

        mgr.challenge_subgoal(1)
        assert mgr.state.checklist[0].status == ITEM_PENDING
        assert len(mgr.state.pending_challenges) == 1
        assert "challenged item 1" in mgr.state.pending_challenges[0]

    def test_challenge_rejects_non_terminal(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="sub-5")
        mgr.set("g")
        mgr.add_subgoal("a")  # still pending
        with pytest.raises(ValueError):
            mgr.challenge_subgoal(1)

    def test_remove_subgoal(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="sub-6")
        mgr.set("g")
        mgr.add_subgoal("a")
        mgr.add_subgoal("b")
        removed = mgr.remove_subgoal(1)
        assert removed.text == "a"
        assert [it.text for it in mgr.state.checklist] == ["b"]

    def test_clear_checklist(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="sub-7")
        mgr.set("g")
        mgr.add_subgoal("a")
        mgr.clear_checklist()
        assert mgr.state.checklist == []
        assert mgr.state.pending_challenges == []


# ──────────────────────────────────────────────────────────────────────
# Continuation prompt — three flavors + challenge prepending
# ──────────────────────────────────────────────────────────────────────


class TestContinuationPrompt:
    def test_decompose_prompt_when_checklist_empty(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="cp-1")
        mgr.set("g")
        prompt = mgr.next_continuation_prompt()
        assert "propose a checklist" in prompt
        assert "goal_checklist" in prompt

    def test_continuation_prompt_with_checklist(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="cp-2")
        mgr.set("g")
        mgr.apply_agent_update(items=[
            {"text": "a"}, {"text": "b"}, {"text": "c"},
        ])
        mgr.apply_agent_update(mark=[{"index": 1, "status": "completed", "evidence": "did a"}])
        prompt = mgr.next_continuation_prompt()
        assert "Checklist progress (1/3 done)" in prompt
        assert "[x] a" in prompt
        assert "[ ] b" in prompt

    def test_pending_challenges_prepend_and_drain(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="cp-3")
        mgr.set("g")
        mgr.apply_agent_update(items=[{"text": "a"}])
        mgr.apply_agent_update(mark=[{"index": 1, "status": "completed", "evidence": "x"}])
        mgr.challenge_subgoal(1)

        prompt = mgr.next_continuation_prompt()
        assert "challenged item 1" in prompt
        assert "Continuing toward your standing goal" in prompt
        # Drained — second call doesn't re-include the challenge
        assert mgr.state.pending_challenges == []
        prompt2 = mgr.next_continuation_prompt()
        assert "challenged item 1" not in prompt2


# ──────────────────────────────────────────────────────────────────────
# goal_checklist model tool
# ──────────────────────────────────────────────────────────────────────


class TestGoalChecklistTool:
    def test_tool_no_active_goal(self, hermes_home):
        from tools.goal_checklist_tool import goal_checklist_tool
        out = goal_checklist_tool(items=[{"text": "x"}], session_id="no-goal-sid")
        data = json.loads(out)
        assert data["ok"] is False
        assert "no active" in data["error"].lower()

    def test_tool_no_session_id(self, hermes_home):
        from tools.goal_checklist_tool import goal_checklist_tool
        out = goal_checklist_tool(items=[{"text": "x"}], session_id="")
        data = json.loads(out)
        assert data["ok"] is False
        assert "session_id" in data["error"]

    def test_tool_seeds_initial_checklist(self, hermes_home):
        from hermes_cli.goals import GoalManager
        from tools.goal_checklist_tool import goal_checklist_tool
        mgr = GoalManager(session_id="tool-1")
        mgr.set("build it")

        out = goal_checklist_tool(
            items=[{"text": "step a"}, {"text": "step b"}],
            session_id="tool-1",
        )
        data = json.loads(out)
        assert data["ok"] is True
        assert data["summary"]["total"] == 2
        assert data["summary"]["pending"] == 2

        # Verify state actually persisted via a fresh manager
        mgr2 = GoalManager(session_id="tool-1")
        assert len(mgr2.state.checklist) == 2

    def test_tool_marks_items(self, hermes_home):
        from hermes_cli.goals import GoalManager
        from tools.goal_checklist_tool import goal_checklist_tool
        mgr = GoalManager(session_id="tool-2")
        mgr.set("g")
        goal_checklist_tool(items=[{"text": "a"}, {"text": "b"}], session_id="tool-2")
        out = goal_checklist_tool(
            mark=[{"index": 1, "status": "completed", "evidence": "did a"}],
            session_id="tool-2",
        )
        data = json.loads(out)
        assert data["ok"] is True
        assert data["summary"]["completed"] == 1
        assert data["summary"]["pending"] == 1

    def test_tool_read_only_call(self, hermes_home):
        from hermes_cli.goals import GoalManager
        from tools.goal_checklist_tool import goal_checklist_tool
        mgr = GoalManager(session_id="tool-3")
        mgr.set("g")
        goal_checklist_tool(items=[{"text": "a"}], session_id="tool-3")
        out = goal_checklist_tool(session_id="tool-3")  # no items, no mark
        data = json.loads(out)
        assert data["ok"] is True
        assert len(data["checklist"]) == 1
        assert data["applied"] == []


# ──────────────────────────────────────────────────────────────────────
# Compression session-rotation: goal must survive the new session_id
# ──────────────────────────────────────────────────────────────────────


class TestGoalSurvivesCompressionRotation:
    def test_load_goal_after_session_id_rotates(self, hermes_home):
        """When auto-compression rotates the session_id, the goal must be
        readable from the new session_id (forwarded by run_agent's
        _compress_context block).
        """
        from hermes_cli.goals import GoalManager
        from hermes_state import SessionDB

        parent_sid = "parent-rotate-001"
        mgr = GoalManager(session_id=parent_sid)
        mgr.set("survive compression")

        db = SessionDB()
        new_sid = "child-rotate-001"
        blob = db.get_meta(f"goal:{parent_sid}")
        assert blob, "goal must be in state_meta"
        db.set_meta(f"goal:{new_sid}", blob)

        mgr2 = GoalManager(session_id=new_sid)
        assert mgr2.is_active()
        assert mgr2.state.goal == "survive compression"
        assert mgr2.state.checklist == mgr.state.checklist
