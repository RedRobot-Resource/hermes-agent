#!/usr/bin/env python3
"""goal_checklist tool — agent-owned standing-goal checklist.

The agent calls this tool to:
  1. Submit the initial checklist when a /goal is set (Phase A).
  2. Mark items completed/impossible as it works (Phase B).
  3. Append items it discovers along the way.

There is no aux-model judge in the loop. The agent that's actually
doing the work owns the progress signal. The user audits via
``/subgoal`` slash commands.

Behavior
--------
- ``items``: write a fresh checklist (initial decompose, or replacement
  when the user has cleared the existing one). Each item is
  ``{text: str, status?: pending|completed|impossible, evidence?: str}``.
- ``mark``: per-item flips ``[{index: int (1-based), status: str,
  evidence?: str}, ...]``. Stickiness in code: agent can flip pending →
  terminal but cannot regress its own terminal items. Only the user can
  via ``/subgoal undo`` or ``/subgoal challenge``.

Both params are optional — call with ``items`` to seed, ``mark`` to
update, both for a one-shot bulk update. Returns the full current
checklist + summary counters.

Wiring
------
The tool is dispatched via ``run_agent.handle_function_call`` (see the
``goal_checklist`` branch in run_agent.py). It needs the live agent's
``session_id`` to load/save state via ``hermes_cli.goals.GoalManager``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


GOAL_CHECKLIST_SCHEMA = {
    "name": "goal_checklist",
    "description": (
        "Manage the checklist for the user's standing /goal. The user has "
        "asked you to work toward a goal across multiple turns; this tool "
        "is how you communicate progress.\n\n"
        "When a goal is set, the system asks you to propose a detailed "
        "checklist of completion criteria. Submit it by calling this tool "
        "with the ``items`` parameter — each item should be a concrete, "
        "verifiable thing (not a vague intention).\n\n"
        "On subsequent turns, mark items completed (or impossible) as you "
        "finish them by passing ``mark``. Always include one-line evidence "
        "citing the specific tool call, file, or output that satisfies the "
        "item. Once an item is in a terminal status (completed or "
        "impossible), it is sticky — only the user can flip it back via "
        "/subgoal commands.\n\n"
        "When every item is in a terminal status, the goal completes and "
        "the loop ends.\n\n"
        "Do NOT mark items completed without doing the work. The user can "
        "challenge any over-claim via /subgoal challenge N, which flips the "
        "item back to pending and forces you to revisit it.\n\n"
        "You may pass both ``items`` and ``mark`` in the same call. With no "
        "params, the tool reads the current checklist."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": (
                    "Submit or replace the checklist. Use this once at the "
                    "start of a new goal to seed the criteria, or when the "
                    "user has cleared the checklist and asked you to "
                    "re-propose. To append a single item to an existing "
                    "checklist, include it here — duplicates by exact text "
                    "are skipped."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": (
                                "Concrete, verifiable completion criterion."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "completed", "impossible"],
                            "description": (
                                "Initial status. Defaults to pending; only "
                                "set this when an item is already done or "
                                "impossible at decompose time."
                            ),
                        },
                        "evidence": {
                            "type": "string",
                            "description": (
                                "One-line citation of why this item is in "
                                "its initial status, when not pending."
                            ),
                        },
                    },
                    "required": ["text"],
                },
            },
            "mark": {
                "type": "array",
                "description": (
                    "Per-item status flips. Each entry has a 1-based "
                    "``index`` into the current checklist, a ``status`` "
                    "(must be 'completed' or 'impossible'), and a one-line "
                    "``evidence`` citing the specific work that satisfies "
                    "or blocks the item."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "1-based checklist index.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["completed", "impossible"],
                        },
                        "evidence": {
                            "type": "string",
                            "description": (
                                "One-line citation of why this item is "
                                "done or impossible. Reference your actual "
                                "tool calls or output."
                            ),
                        },
                    },
                    "required": ["index", "status", "evidence"],
                },
            },
        },
        "required": [],
    },
}


def goal_checklist_tool(
    items: Optional[List[Dict[str, Any]]] = None,
    mark: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
    default_max_turns: int = 20,
) -> str:
    """Single entry point for the goal_checklist tool.

    Args:
        items: optional initial / append list of checklist items.
        mark: optional per-item terminal-status flips.
        session_id: the live agent's session_id (used to load/save the
            GoalState row).
        default_max_turns: budget passed through to GoalManager when it
            instantiates a new manager. Pulled from config by the
            run_agent dispatch site.

    Returns:
        JSON string with the full current checklist + summary counters,
        or an error message if no active goal exists.
    """
    if not session_id:
        return json.dumps(
            {"ok": False, "error": "no session_id — cannot resolve goal state"},
            ensure_ascii=False,
        )

    try:
        from hermes_cli.goals import GoalManager
    except Exception as exc:
        logger.debug("goal_checklist: GoalManager import failed: %s", exc)
        return json.dumps(
            {"ok": False, "error": f"goal manager unavailable: {exc}"},
            ensure_ascii=False,
        )

    mgr = GoalManager(session_id=session_id, default_max_turns=default_max_turns)

    if not mgr.has_goal():
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "No active /goal for this session. The user has not set "
                    "a standing goal — this tool is only useful when /goal "
                    "is active."
                ),
            },
            ensure_ascii=False,
        )

    # Read-only call: just return the current state.
    if items is None and not mark:
        state = mgr.state
        if state is None:
            return json.dumps(
                {"ok": False, "error": "goal state missing"}, ensure_ascii=False
            )
        cl_total, cl_done, cl_imp, cl_pending = state.checklist_counts()
        return json.dumps(
            {
                "ok": True,
                "applied": [],
                "checklist": [it.to_dict() for it in state.checklist],
                "summary": {
                    "total": cl_total,
                    "completed": cl_done,
                    "impossible": cl_imp,
                    "pending": cl_pending,
                    "all_terminal": state.all_terminal(),
                },
            },
            ensure_ascii=False,
        )

    try:
        result = mgr.apply_agent_update(items=items, mark=mark)
    except RuntimeError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    return json.dumps(result, ensure_ascii=False)


def check_goal_checklist_requirements() -> bool:
    """Always available — no external dependencies."""
    return True


# --- Registry ---
from tools.registry import registry  # noqa: E402

registry.register(
    name="goal_checklist",
    toolset="todo",
    schema=GOAL_CHECKLIST_SCHEMA,
    # The dispatch site in run_agent.py injects ``session_id`` and
    # ``default_max_turns`` from the AIAgent instance — this lambda is
    # kept as a fallback that returns an error when called without that
    # context (e.g., unit tests that bypass the dispatch).
    handler=lambda args, **kw: goal_checklist_tool(
        items=args.get("items"),
        mark=args.get("mark"),
        session_id=kw.get("session_id"),
        default_max_turns=kw.get("default_max_turns", 20),
    ),
    check_fn=check_goal_checklist_requirements,
    emoji="⊙",
)
