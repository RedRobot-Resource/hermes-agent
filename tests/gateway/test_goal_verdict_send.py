"""Tests for gateway /goal verdict-message delivery.

The status messages ("✓ Goal achieved", "⏸ budget exhausted",
"↻ Continuing toward goal") must reach the user after each turn.
Before this fix the code checked ``hasattr(adapter, "send_message")``
— but adapters expose ``send()``, never ``send_message``, so the check
always evaluated False and users never saw verdicts. This test locks
in the fix.

Updated for the agent-owned checklist design: there is no aux-model
judge to mock. State is driven directly via GoalManager.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


class _RecordingAdapter:
    """Minimal adapter that records send() invocations."""

    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self.sends: list[dict] = []

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        self.sends.append({"chat_id": chat_id, "content": content, "metadata": metadata})

        class _R:
            success = True
            message_id = "mock-msg"

        return _R()


def _make_runner_with_adapter(session_id: str = None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._queued_events = {}

    src = _make_source()
    session_entry = SessionEntry(
        session_key=build_session_key(src),
        session_id=session_id or f"goal-sess-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._generate_session_key.return_value = build_session_key(src)

    adapter = _RecordingAdapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter, session_entry, src


@pytest.mark.asyncio
async def test_goal_verdict_done_sent_via_adapter_send(hermes_home):
    """When all checklist items terminal, '✓ Goal achieved' must reach the
    user through the adapter's ``send()`` method."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import (
        GoalManager, ChecklistItem, ITEM_COMPLETED, ADDED_BY_AGENT, save_goal,
    )

    mgr = GoalManager(session_entry.session_id)
    mgr.set("ship the feature")
    # Drive the state directly: agent marked all items terminal.
    mgr.state.checklist = [
        ChecklistItem(text="a", status=ITEM_COMPLETED, added_by=ADDED_BY_AGENT),
    ]
    save_goal(session_entry.session_id, mgr.state)

    await runner._post_turn_goal_continuation(
        session_entry=session_entry,
        source=src,
        final_response="I shipped the feature.",
    )
    await asyncio.sleep(0.05)

    assert len(adapter.sends) == 1, f"expected 1 send, got {len(adapter.sends)}: {adapter.sends}"
    msg = adapter.sends[0]
    assert msg["chat_id"] == "c1"
    assert "Goal achieved" in msg["content"]


@pytest.mark.asyncio
async def test_goal_verdict_continue_enqueues_continuation(hermes_home):
    """Active goal with pending items → 'Continuing toward goal' status sent
    AND continuation prompt enqueued for the next turn."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_entry.session_id)
    mgr.set("polish the docs")
    # Empty checklist — first turn after /goal, continuation prompt is the
    # decompose prompt asking the agent to call goal_checklist.

    await runner._post_turn_goal_continuation(
        session_entry=session_entry,
        source=src,
        final_response="here's a partial edit",
    )
    await asyncio.sleep(0.05)

    assert len(adapter.sends) == 1
    assert "Continuing toward goal" in adapter.sends[0]["content"]
    assert adapter._pending_messages, "continuation prompt must be enqueued in pending_messages"


@pytest.mark.asyncio
async def test_goal_verdict_budget_exhausted_sends_pause(hermes_home):
    """When the budget is exhausted, a '⏸ Goal paused' message must be sent
    and no further continuation enqueued."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager, save_goal

    mgr = GoalManager(session_entry.session_id, default_max_turns=2)
    state = mgr.set("tiny goal", max_turns=2)
    state.turns_used = 1  # next call increments to 2 == max_turns → paused
    save_goal(session_entry.session_id, state)

    await runner._post_turn_goal_continuation(
        session_entry=session_entry,
        source=src,
        final_response="still partial",
    )
    await asyncio.sleep(0.05)

    assert len(adapter.sends) == 1
    content = adapter.sends[0]["content"]
    assert "paused" in content.lower()
    assert "turns used" in content.lower()
    assert not adapter._pending_messages


@pytest.mark.asyncio
async def test_goal_verdict_skipped_when_no_active_goal(hermes_home):
    """No goal set → the hook is a no-op. Nothing is sent, nothing enqueued."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    await runner._post_turn_goal_continuation(
        session_entry=session_entry,
        source=src,
        final_response="anything",
    )
    await asyncio.sleep(0.05)

    assert adapter.sends == []
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_goal_verdict_survives_adapter_without_send(hermes_home):
    """Bad adapter (no ``send`` attribute) must not crash the goal hook."""
    runner, _adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import (
        GoalManager, ChecklistItem, ITEM_COMPLETED, ADDED_BY_AGENT, save_goal,
    )

    mgr = GoalManager(session_entry.session_id)
    mgr.set("survive missing send")
    mgr.state.checklist = [
        ChecklistItem(text="a", status=ITEM_COMPLETED, added_by=ADDED_BY_AGENT),
    ]
    save_goal(session_entry.session_id, mgr.state)

    class _NoSendAdapter:
        def __init__(self):
            self._pending_messages: dict = {}

    runner.adapters[Platform.TELEGRAM] = _NoSendAdapter()

    # must not raise
    await runner._post_turn_goal_continuation(
        session_entry=session_entry,
        source=src,
        final_response="whatever",
    )
    await asyncio.sleep(0.05)
