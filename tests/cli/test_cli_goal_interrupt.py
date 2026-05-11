"""Tests for CLI goal-continuation hook behavior.

Covers:
- Ctrl+C during a /goal turn auto-pauses the goal (no more continuations).
- Clean turn with empty checklist re-queues a continuation (decompose path).
- Checklist all-terminal marks the goal done.
- Budget exhaustion pauses the loop.

These tests exercise ``_maybe_continue_goal_after_turn`` directly on a
minimal ``HermesCLI`` stub. There is no aux-model judge in the new
design — the agent owns the checklist via the ``goal_checklist`` model
tool, and the hook just inspects state.
"""

from __future__ import annotations

import queue
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so SessionDB.state_meta writes stay hermetic."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _make_cli_with_goal(session_id: str, goal_text: str = "build a thing"):
    """Build a minimal HermesCLI stub with an active goal wired in."""
    from cli import HermesCLI
    from hermes_cli.goals import GoalManager

    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False
    cli.conversation_history = []
    cli.session_id = session_id
    cli.agent = MagicMock()
    cli.agent.session_id = session_id

    mgr = GoalManager(session_id=session_id, default_max_turns=5)
    mgr.set(goal_text)
    cli._goal_manager = mgr
    return cli, mgr


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────


class TestInterruptAutoPause:
    def test_interrupted_turn_pauses_goal_and_skips_continuation(self, hermes_home):
        """Ctrl+C mid-turn must auto-pause the goal, not queue another round."""
        sid = f"sid-interrupt-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        cli._last_turn_interrupted = True
        cli.conversation_history = [
            {"role": "user", "content": "kickoff"},
            {"role": "assistant", "content": "starting work..."},
        ]

        cli._maybe_continue_goal_after_turn()

        assert cli._pending_input.empty(), (
            "Interrupted turn should not enqueue a continuation prompt"
        )
        assert mgr.state.status == "paused"
        assert "interrupt" in (mgr.state.paused_reason or "").lower()

    def test_interrupted_turn_is_resumable(self, hermes_home):
        """After auto-pause from Ctrl+C, /goal resume puts it back to active."""
        sid = f"sid-resume-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        cli._last_turn_interrupted = True
        cli.conversation_history = [
            {"role": "assistant", "content": "partial"},
        ]
        cli._maybe_continue_goal_after_turn()
        assert mgr.state.status == "paused"

        mgr.resume()
        assert mgr.state.status == "active"


class TestPendingUserInputPreemption:
    def test_pending_user_input_skips_continuation(self, hermes_home):
        """If a real user message is already queued, the goal hook
        defers — the user's turn takes priority."""
        sid = f"sid-preempt-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        cli._pending_input.put("a real user message")
        cli._maybe_continue_goal_after_turn()
        # Hook should be a no-op; the user message stays in front and no
        # continuation is appended.
        assert cli._pending_input.qsize() == 1
        # turn budget not consumed
        assert mgr.state.turns_used == 0


class TestHealthyTurn:
    def test_empty_checklist_enqueues_decompose_prompt(self, hermes_home):
        """First turn after /goal: checklist empty → continuation prompt is
        the decompose prompt asking the agent to call goal_checklist."""
        sid = f"sid-healthy-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        cli.conversation_history = [
            {"role": "user", "content": "kickoff"},
            {"role": "assistant", "content": "starting"},
        ]

        cli._maybe_continue_goal_after_turn()

        assert not cli._pending_input.empty()
        queued = cli._pending_input.get_nowait()
        assert "propose a checklist" in queued
        assert "goal_checklist" in queued
        assert mgr.state.status == "active"
        assert mgr.state.turns_used == 1

    def test_partial_checklist_enqueues_continuation(self, hermes_home):
        """Some items pending → continuation prompt has the progress block."""
        from hermes_cli.goals import (
            ChecklistItem, ITEM_PENDING, ITEM_COMPLETED, ADDED_BY_AGENT,
        )
        from hermes_cli import goals as _g
        sid = f"sid-partial-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        mgr.state.checklist = [
            ChecklistItem(text="a", status=ITEM_COMPLETED, added_by=ADDED_BY_AGENT),
            ChecklistItem(text="b", status=ITEM_PENDING, added_by=ADDED_BY_AGENT),
        ]
        _g.save_goal(sid, mgr.state)

        cli._maybe_continue_goal_after_turn()

        assert not cli._pending_input.empty()
        queued = cli._pending_input.get_nowait()
        assert "Checklist progress (1/2 done)" in queued
        assert "[x] a" in queued
        assert "[ ] b" in queued

    def test_all_terminal_marks_done(self, hermes_home):
        """All items terminal → status=done, no continuation."""
        from hermes_cli.goals import (
            ChecklistItem, ITEM_COMPLETED, ITEM_IMPOSSIBLE, ADDED_BY_AGENT,
        )
        from hermes_cli import goals as _g
        sid = f"sid-done-{uuid.uuid4().hex}"
        cli, mgr = _make_cli_with_goal(sid)
        mgr.state.checklist = [
            ChecklistItem(text="a", status=ITEM_COMPLETED, added_by=ADDED_BY_AGENT),
            ChecklistItem(text="b", status=ITEM_IMPOSSIBLE, added_by=ADDED_BY_AGENT),
        ]
        _g.save_goal(sid, mgr.state)

        cli._maybe_continue_goal_after_turn()

        assert cli._pending_input.empty()
        assert mgr.state.status == "done"


class TestInterruptFlagLifecycle:
    def test_chat_resets_flag_at_entry(self, hermes_home):
        """chat() must reset _last_turn_interrupted at the top of each turn.

        This guards against stale flag state: if turn N was interrupted and
        turn N+1 runs clean, the hook must not see True from N.
        """
        from cli import HermesCLI
        import inspect

        src = inspect.getsource(HermesCLI.chat)
        head = src.split("if not self._ensure_runtime_credentials", 1)[0]
        assert "self._last_turn_interrupted = False" in head, (
            "chat() must reset _last_turn_interrupted before run_conversation "
            "runs — otherwise a prior turn's interrupt state leaks into the "
            "next turn's goal hook decision."
        )
