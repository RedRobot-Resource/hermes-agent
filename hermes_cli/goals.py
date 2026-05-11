"""Persistent session goals — agent-owned checklist.

A goal is a free-form user objective that stays active across turns. The
agent that's actually doing the work owns the progress signal: it
proposes a checklist of completion criteria when the goal is set, then
calls the ``goal_checklist`` model tool to mark items completed (or
impossible) as it works. The continuation loop terminates when every
item is in a terminal status, the user pauses/clears, or the turn budget
hits.

There is **no aux-model judge** in the loop. The previous design routed
each turn through an auxiliary "is the goal done?" call, which suffered
from snippet-only vision, dump-file size limits, JSON-output drift, and
evidence misalignment. The agent has full visibility into its own tool
calls, file writes, and command output — let it self-report and let the
user audit via ``/subgoal``.

State is persisted in SessionDB's ``state_meta`` table keyed by
``goal:<session_id>`` so ``/resume`` picks it up.

Design notes / invariants:

- The continuation prompt is a normal user message appended to the
  session via ``run_conversation``. No system-prompt mutation, no
  toolset swap — prompt caching stays intact.
- The agent is the only authority that flips ``pending → completed |
  impossible``. The user can override via ``/subgoal complete N`` /
  ``impossible N`` / ``undo N`` / ``challenge N`` (which flips back to
  pending AND injects a user-role nudge so the agent revisits).
- When a real user message arrives mid-loop it preempts the
  continuation prompt and also pauses the goal loop for that turn.
- This module has zero hard dependency on ``cli.HermesCLI`` or the
  gateway runner — both wire the same ``GoalManager`` in.

Nothing in this module touches the agent's system prompt or toolset.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants & defaults
# ──────────────────────────────────────────────────────────────────────

DEFAULT_MAX_TURNS = 20

# Status constants ────────────────────────────────────────────────────
ITEM_PENDING = "pending"
ITEM_COMPLETED = "completed"
ITEM_IMPOSSIBLE = "impossible"
TERMINAL_ITEM_STATUSES = frozenset({ITEM_COMPLETED, ITEM_IMPOSSIBLE})
VALID_ITEM_STATUSES = frozenset({ITEM_PENDING, ITEM_COMPLETED, ITEM_IMPOSSIBLE})

ITEM_MARKERS = {
    ITEM_COMPLETED: "[x]",
    ITEM_IMPOSSIBLE: "[!]",
    ITEM_PENDING: "[ ]",
}

ADDED_BY_AGENT = "agent"
ADDED_BY_USER = "user"


# ──────────────────────────────────────────────────────────────────────
# Continuation prompts
# ──────────────────────────────────────────────────────────────────────

# When no checklist exists yet (decompose hasn't run), kick the agent into
# proposing one. The model tool ``goal_checklist`` is the entry point.
DECOMPOSE_PROMPT_TEMPLATE = (
    "[Standing goal — please propose a checklist before starting]\n"
    "Goal: {goal}\n\n"
    "Before doing any other work, decompose this goal into a detailed "
    "checklist of concrete, verifiable completion criteria. Each item "
    "should be a specific, checkable thing — not a vague intention. Aim "
    "for at least 5 items; more is better when warranted. Include "
    "sub-items, edge cases, quality bars, and verification steps.\n\n"
    "Submit the checklist by calling the ``goal_checklist`` tool with "
    "``items`` set to the list of criteria. Then start working on the "
    "items. Mark each item ``completed`` (with one-line evidence) by "
    "calling ``goal_checklist`` again with the ``mark`` parameter as "
    "you finish it. Mark items ``impossible`` (with reason) when they "
    "cannot be achieved in this environment.\n\n"
    "When every item is in a terminal status the goal is done."
)

# Used on subsequent turns when a checklist already exists. The agent
# sees the current state and continues working.
CONTINUATION_PROMPT_WITH_CHECKLIST_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Checklist progress ({done}/{total} done):\n"
    "{checklist}\n\n"
    "Take the next concrete step on a pending item. As you complete "
    "items, call the ``goal_checklist`` tool with the ``mark`` "
    "parameter to flip them to ``completed`` (with one-line evidence) "
    "or ``impossible`` (with reason). If you are blocked on a remaining "
    "item and need user input, say so clearly and stop.\n\n"
    "Do not over-claim — only mark items completed when they are "
    "actually done. The user can challenge any item via ``/subgoal "
    "challenge N`` if they think you over-claimed."
)

# Fallback for goals that somehow have no checklist (decompose was
# cleared by the user, or an old back-compat row). Plain Ralph loop.
CONTINUATION_PROMPT_NO_CHECKLIST_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Continue working toward this goal. Take the next concrete step. "
    "If you believe the goal is complete, call the ``goal_checklist`` "
    "tool with a single completed item summarizing what you did. If "
    "you are blocked and need user input, say so clearly and stop."
)

# Back-compat alias for callers (gateway tests + builtin hooks) that
# reference the old single-template name. Points at the no-checklist
# template since it's the closest analogue to the original prompt.
CONTINUATION_PROMPT_TEMPLATE = CONTINUATION_PROMPT_NO_CHECKLIST_TEMPLATE

# When the user uses ``/subgoal challenge N``, this user-role message is
# injected at the next turn so the agent revisits the item.
CHALLENGE_PROMPT_TEMPLATE = (
    "[User challenge on standing-goal checklist]\n"
    "The user has challenged item {index} ({text!r}). It was previously "
    "marked {prev_status} but the user disagrees and has flipped it back "
    "to pending. Address their concern: revisit the work, produce "
    "additional evidence, or explain why the item is genuinely "
    "impossible. Do not silently re-mark the item completed without new "
    "evidence."
)


# ──────────────────────────────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ChecklistItem:
    """One concrete completion criterion attached to a goal."""

    text: str
    status: str = ITEM_PENDING            # pending | completed | impossible
    added_by: str = ADDED_BY_AGENT        # agent | user
    added_at: float = 0.0
    completed_at: Optional[float] = None
    evidence: Optional[str] = None        # one-line citation

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChecklistItem":
        text = str(data.get("text", "")).strip()
        if not text:
            text = "(empty item)"
        status = str(data.get("status", ITEM_PENDING)).strip().lower()
        if status not in VALID_ITEM_STATUSES:
            status = ITEM_PENDING
        added_by = str(data.get("added_by", ADDED_BY_AGENT)).strip().lower()
        if added_by not in (ADDED_BY_AGENT, ADDED_BY_USER):
            added_by = ADDED_BY_AGENT
        return cls(
            text=text,
            status=status,
            added_by=added_by,
            added_at=float(data.get("added_at", 0.0) or 0.0),
            completed_at=(
                float(data["completed_at"])
                if data.get("completed_at") is not None
                else None
            ),
            evidence=data.get("evidence"),
        )


@dataclass
class GoalState:
    """Serializable goal state stored per session.

    Backward-compatible with the prior judge-loop schema: missing fields
    default safely so old ``state_meta`` rows still load.
    """

    goal: str
    status: str = "active"            # active | paused | done | cleared
    turns_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    created_at: float = 0.0
    last_turn_at: float = 0.0
    paused_reason: Optional[str] = None
    checklist: List[ChecklistItem] = field(default_factory=list)
    # Pending user-challenge nudges to inject on the next continuation
    # turn (each is a complete user-role message body).
    pending_challenges: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "GoalState":
        data = json.loads(raw)
        raw_checklist = data.get("checklist") or []
        checklist: List[ChecklistItem] = []
        if isinstance(raw_checklist, list):
            for item in raw_checklist:
                if isinstance(item, dict):
                    try:
                        checklist.append(ChecklistItem.from_dict(item))
                    except Exception:
                        continue
        raw_challenges = data.get("pending_challenges") or []
        pending_challenges: List[str] = []
        if isinstance(raw_challenges, list):
            pending_challenges = [str(c) for c in raw_challenges if c]
        return cls(
            goal=data.get("goal", ""),
            status=data.get("status", "active"),
            turns_used=int(data.get("turns_used", 0) or 0),
            max_turns=int(
                data.get("max_turns", DEFAULT_MAX_TURNS) or DEFAULT_MAX_TURNS
            ),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            last_turn_at=float(data.get("last_turn_at", 0.0) or 0.0),
            paused_reason=data.get("paused_reason"),
            checklist=checklist,
            pending_challenges=pending_challenges,
        )

    # --- checklist helpers ------------------------------------------------

    def checklist_counts(self) -> tuple:
        """Return (total, completed, impossible, pending)."""
        total = len(self.checklist)
        completed = sum(1 for it in self.checklist if it.status == ITEM_COMPLETED)
        impossible = sum(1 for it in self.checklist if it.status == ITEM_IMPOSSIBLE)
        pending = total - completed - impossible
        return total, completed, impossible, pending

    def all_terminal(self) -> bool:
        """True iff at least one item exists and every item is in a terminal status."""
        if not self.checklist:
            return False
        return all(it.status in TERMINAL_ITEM_STATUSES for it in self.checklist)

    def render_checklist(self, *, numbered: bool = False) -> str:
        if not self.checklist:
            return "(empty)"
        lines = []
        for i, item in enumerate(self.checklist, start=1):
            marker = ITEM_MARKERS.get(item.status, "[?]")
            prefix = f"{i}. {marker}" if numbered else f"  {marker}"
            line = f"{prefix} {item.text}"
            if item.status == ITEM_COMPLETED and item.evidence:
                line += f" — {item.evidence}"
            elif item.status == ITEM_IMPOSSIBLE and item.evidence:
                line += f" (impossible: {item.evidence})"
            lines.append(line)
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Persistence (SessionDB state_meta)
# ──────────────────────────────────────────────────────────────────────


def _meta_key(session_id: str) -> str:
    return f"goal:{session_id}"


_DB_CACHE: Dict[str, Any] = {}


def _get_session_db() -> Optional[Any]:
    """Return a SessionDB instance for the current HERMES_HOME.

    SessionDB has no built-in singleton, but opening a new connection per
    /goal call would thrash the file. We cache one instance per
    ``hermes_home`` path so profile switches still pick up the right DB.
    Defensive against import/instantiation failures so tests and
    non-standard launchers can still use the GoalManager.
    """
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB

        home = str(get_hermes_home())
    except Exception as exc:  # pragma: no cover
        logger.debug("GoalManager: SessionDB bootstrap failed (%s)", exc)
        return None

    cached = _DB_CACHE.get(home)
    if cached is not None:
        return cached
    try:
        db = SessionDB()
    except Exception as exc:  # pragma: no cover
        logger.debug("GoalManager: SessionDB() raised (%s)", exc)
        return None
    _DB_CACHE[home] = db
    return db


def load_goal(session_id: str) -> Optional[GoalState]:
    """Load the goal for a session, or None if none exists."""
    if not session_id:
        return None
    db = _get_session_db()
    if db is None:
        return None
    try:
        raw = db.get_meta(_meta_key(session_id))
    except Exception as exc:
        logger.debug("GoalManager: get_meta failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return GoalState.from_json(raw)
    except Exception as exc:
        logger.warning(
            "GoalManager: could not parse stored goal for %s: %s", session_id, exc
        )
        return None


def save_goal(session_id: str, state: GoalState) -> None:
    """Persist a goal to SessionDB. No-op if DB unavailable."""
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        return
    try:
        db.set_meta(_meta_key(session_id), state.to_json())
    except Exception as exc:
        logger.debug("GoalManager: set_meta failed: %s", exc)


def clear_goal(session_id: str) -> None:
    """Mark a goal cleared in the DB (preserved for audit, status=cleared)."""
    state = load_goal(session_id)
    if state is None:
        return
    state.status = "cleared"
    save_goal(session_id, state)


# ──────────────────────────────────────────────────────────────────────
# GoalManager — the orchestration surface CLI + gateway talk to
# ──────────────────────────────────────────────────────────────────────


class GoalManager:
    """Per-session goal state + continuation decisions.

    The CLI and gateway each hold one ``GoalManager`` per live session.

    Methods:

    - ``set(goal)`` — start a new standing goal.
    - ``clear()`` — remove the active goal.
    - ``pause()`` / ``resume()`` — explicit user controls.
    - ``status_line()`` — printable one-liner.
    - ``add_subgoal(text)`` — user appends a checklist item.
    - ``mark_subgoal(index, status)`` — user override.
    - ``challenge_subgoal(index)`` — flip terminal → pending and queue
      a user-role nudge for the next turn.
    - ``remove_subgoal(index)`` — delete an item.
    - ``clear_checklist()`` — wipe the checklist; agent re-decomposes
      on the next turn.
    - ``apply_agent_update(items=, mark=)`` — invoked by the
      ``goal_checklist`` model tool handler. Replaces or extends the
      checklist (``items``) and/or flips item statuses (``mark``).
    - ``evaluate_after_turn(...)`` — check termination, return a
      decision dict the caller uses to drive the next turn.
    - ``next_continuation_prompt()`` — the canonical user-role message
      to feed back into ``run_conversation``.
    """

    def __init__(self, session_id: str, *, default_max_turns: int = DEFAULT_MAX_TURNS):
        self.session_id = session_id
        self.default_max_turns = int(default_max_turns or DEFAULT_MAX_TURNS)
        self._state: Optional[GoalState] = load_goal(session_id)

    # --- introspection ------------------------------------------------

    @property
    def state(self) -> Optional[GoalState]:
        return self._state

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_goal(self) -> bool:
        return self._state is not None and self._state.status in ("active", "paused")

    def status_line(self) -> str:
        s = self._state
        if s is None or s.status == "cleared":
            return "No active goal. Set one with /goal <text>."
        turns = f"{s.turns_used}/{s.max_turns} turns"
        cl_total, cl_done, cl_imp, _ = s.checklist_counts()
        cl_text = ""
        if cl_total:
            cl_text = f", {cl_done + cl_imp}/{cl_total} done"
        if s.status == "active":
            return f"⊙ Goal (active, {turns}{cl_text}): {s.goal}"
        if s.status == "paused":
            extra = f" — {s.paused_reason}" if s.paused_reason else ""
            return f"⏸ Goal (paused, {turns}{cl_text}{extra}): {s.goal}"
        if s.status == "done":
            return f"✓ Goal done ({turns}{cl_text}): {s.goal}"
        return f"Goal ({s.status}, {turns}{cl_text}): {s.goal}"

    def render_checklist(self) -> str:
        """Public helper for the /subgoal slash command."""
        if self._state is None:
            return "(no active goal)"
        if not self._state.checklist:
            return (
                "(checklist empty — agent will propose one on the next turn)"
            )
        return self._state.render_checklist(numbered=True)

    # --- mutation -----------------------------------------------------

    def set(self, goal: str, *, max_turns: Optional[int] = None) -> GoalState:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal text is empty")
        state = GoalState(
            goal=goal,
            status="active",
            turns_used=0,
            max_turns=int(max_turns) if max_turns else self.default_max_turns,
            created_at=time.time(),
            last_turn_at=0.0,
            checklist=[],
            pending_challenges=[],
        )
        self._state = state
        save_goal(self.session_id, state)
        return state

    def pause(self, reason: str = "user-paused") -> Optional[GoalState]:
        if not self._state:
            return None
        self._state.status = "paused"
        self._state.paused_reason = reason
        save_goal(self.session_id, self._state)
        return self._state

    def resume(self, *, reset_budget: bool = True) -> Optional[GoalState]:
        if not self._state:
            return None
        self._state.status = "active"
        self._state.paused_reason = None
        if reset_budget:
            self._state.turns_used = 0
        save_goal(self.session_id, self._state)
        return self._state

    def clear(self) -> None:
        if self._state is None:
            return
        self._state.status = "cleared"
        save_goal(self.session_id, self._state)
        self._state = None

    # --- /subgoal user controls ---------------------------------------

    def add_subgoal(self, text: str) -> ChecklistItem:
        """User appends a new checklist item. Requires an active or paused goal."""
        if self._state is None:
            raise RuntimeError("no active goal")
        text = (text or "").strip()
        if not text:
            raise ValueError("subgoal text is empty")
        item = ChecklistItem(
            text=text,
            status=ITEM_PENDING,
            added_by=ADDED_BY_USER,
            added_at=time.time(),
        )
        self._state.checklist.append(item)
        save_goal(self.session_id, self._state)
        return item

    def mark_subgoal(self, index_1based: int, status: str) -> ChecklistItem:
        """User overrides an item's status.

        ``status`` may be ``completed``, ``impossible``, or ``pending``
        (the last as an undo flow). User actions bypass any stickiness —
        the user is the final authority on what's done.
        """
        if self._state is None:
            raise RuntimeError("no active goal")
        status = (status or "").strip().lower()
        if status not in VALID_ITEM_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(VALID_ITEM_STATUSES)}; got {status!r}"
            )
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(self._state.checklist):
            raise IndexError(
                f"index out of range (1..{len(self._state.checklist)})"
            )
        item = self._state.checklist[idx]
        item.status = status
        if status in TERMINAL_ITEM_STATUSES:
            item.completed_at = time.time()
            if not item.evidence:
                item.evidence = "marked by user"
        else:
            item.completed_at = None
            # Don't wipe agent-supplied evidence on undo — useful audit trail.
        save_goal(self.session_id, self._state)
        return item

    def challenge_subgoal(self, index_1based: int) -> ChecklistItem:
        """Flip an item back to pending AND queue a user-role nudge so
        the agent revisits it on the next turn.

        Only valid on items in a terminal status — there's nothing to
        challenge on a pending item.
        """
        if self._state is None:
            raise RuntimeError("no active goal")
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(self._state.checklist):
            raise IndexError(
                f"index out of range (1..{len(self._state.checklist)})"
            )
        item = self._state.checklist[idx]
        if item.status not in TERMINAL_ITEM_STATUSES:
            raise ValueError(
                f"item {index_1based} is {item.status} — nothing to challenge"
            )
        prev_status = item.status
        item.status = ITEM_PENDING
        item.completed_at = None
        # Keep the agent's prior evidence on the item as audit trail —
        # the next-turn nudge calls it out.
        nudge = CHALLENGE_PROMPT_TEMPLATE.format(
            index=index_1based,
            text=item.text,
            prev_status=prev_status,
        )
        self._state.pending_challenges.append(nudge)
        save_goal(self.session_id, self._state)
        return item

    def remove_subgoal(self, index_1based: int) -> ChecklistItem:
        if self._state is None:
            raise RuntimeError("no active goal")
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(self._state.checklist):
            raise IndexError(
                f"index out of range (1..{len(self._state.checklist)})"
            )
        removed = self._state.checklist.pop(idx)
        save_goal(self.session_id, self._state)
        return removed

    def clear_checklist(self) -> None:
        """Wipe the checklist. Agent will re-propose one on the next turn."""
        if self._state is None:
            return
        self._state.checklist = []
        self._state.pending_challenges = []
        save_goal(self.session_id, self._state)

    # --- agent updates (called from the goal_checklist model tool) ----

    def apply_agent_update(
        self,
        *,
        items: Optional[List[Dict[str, Any]]] = None,
        mark: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Apply agent updates and persist. Returns a summary dict the
        tool handler can return verbatim to the model.

        ``items`` (initial decompose or replacement set):
            Replaces the checklist when present. Each item is
            ``{text: str, status?: pending|completed|impossible,
            evidence?: str}``. User-added items survive at their
            current positions if they aren't already present.

        ``mark`` (per-item flips):
            ``[{index: int (1-based), status: str, evidence?: str}, ...]``.
            Stickiness rules: the agent can flip pending → terminal but
            cannot regress its own terminal items; only the user can.
        """
        if self._state is None:
            raise RuntimeError("no active goal")

        updates_applied: List[str] = []

        # ── items: replace / extend ────────────────────────────────
        if items is not None:
            # If the agent submits a fresh items list AND the existing
            # checklist is empty, this is the initial decompose: store
            # everything.
            #
            # If items are submitted on top of an existing checklist
            # (rare), append the new ones to the end and skip duplicates
            # by exact text match.
            now = time.time()
            new_items: List[ChecklistItem] = []
            for entry in items:
                if not isinstance(entry, dict):
                    if isinstance(entry, str):
                        entry = {"text": entry}
                    else:
                        continue
                text = str(entry.get("text", "")).strip()
                if not text:
                    continue
                status = str(entry.get("status", ITEM_PENDING)).strip().lower()
                if status not in VALID_ITEM_STATUSES:
                    status = ITEM_PENDING
                evidence = str(entry.get("evidence") or "").strip() or None
                ci = ChecklistItem(
                    text=text,
                    status=status,
                    added_by=ADDED_BY_AGENT,
                    added_at=now,
                    completed_at=now if status in TERMINAL_ITEM_STATUSES else None,
                    evidence=evidence,
                )
                new_items.append(ci)

            if not self._state.checklist:
                # Initial decompose
                self._state.checklist = new_items
                updates_applied.append(f"submitted {len(new_items)} items")
            else:
                existing_texts = {it.text.lower() for it in self._state.checklist}
                appended = 0
                for ci in new_items:
                    if ci.text.lower() in existing_texts:
                        continue
                    self._state.checklist.append(ci)
                    existing_texts.add(ci.text.lower())
                    appended += 1
                if appended:
                    updates_applied.append(f"appended {appended} new items")

        # ── mark: per-item flips ──────────────────────────────────
        if mark:
            for upd in mark:
                if not isinstance(upd, dict):
                    continue
                try:
                    idx_1based = int(upd.get("index"))
                except (TypeError, ValueError):
                    continue
                idx = idx_1based - 1
                if idx < 0 or idx >= len(self._state.checklist):
                    continue
                item = self._state.checklist[idx]
                new_status = str(upd.get("status", "")).strip().lower()
                if new_status not in TERMINAL_ITEM_STATUSES:
                    # Agent can only flip to terminal statuses; skip non-terminal.
                    continue
                if item.status in TERMINAL_ITEM_STATUSES:
                    # Stickiness: agent cannot regress its own terminal items.
                    # Only user can via /subgoal undo or /subgoal challenge.
                    continue
                item.status = new_status
                item.completed_at = time.time()
                evidence = str(upd.get("evidence") or "").strip() or None
                if evidence:
                    item.evidence = evidence
                updates_applied.append(f"item {idx_1based} → {new_status}")

        save_goal(self.session_id, self._state)

        cl_total, cl_done, cl_imp, cl_pending = self._state.checklist_counts()
        return {
            "ok": True,
            "applied": updates_applied,
            "checklist": [it.to_dict() for it in self._state.checklist],
            "summary": {
                "total": cl_total,
                "completed": cl_done,
                "impossible": cl_imp,
                "pending": cl_pending,
                "all_terminal": self._state.all_terminal(),
            },
        }

    # --- the main entry point called after every turn -----------------

    def evaluate_after_turn(
        self,
        last_response: str = "",
        *,
        user_initiated: bool = True,
    ) -> Dict[str, Any]:
        """Decide whether the loop should continue. NO judge call.

        We just check the agent-owned checklist:
        - all items terminal → status=done
        - turn budget hit → paused
        - else → continue and emit the next continuation prompt

        ``last_response`` and ``user_initiated`` are accepted for API
        parity with the old judge-loop signature; we don't need them.

        Decision keys:
          - ``status``: current goal status after update
          - ``should_continue``: bool — caller should fire another turn
          - ``continuation_prompt``: str or None
          - ``message``: user-visible one-liner to print/send
        """
        # Reload state from SessionDB before evaluating. The agent may have
        # written checklist updates via the goal_checklist tool during the
        # turn that just finished; those writes go through a separate
        # GoalManager instance, so our cached ``self._state`` is stale.
        # Without this reload, ``all_terminal()`` returns False on a fully-
        # completed checklist and the loop spins.
        self._state = load_goal(self.session_id)

        state = self._state
        if state is None or state.status != "active":
            return {
                "status": state.status if state else None,
                "should_continue": False,
                "continuation_prompt": None,
                "message": "",
            }

        # Count the turn that just finished.
        state.turns_used += 1
        state.last_turn_at = time.time()

        # 1) Did the agent complete the goal?
        if state.all_terminal():
            state.status = "done"
            save_goal(self.session_id, state)
            cl_total, cl_done, cl_imp, _ = state.checklist_counts()
            return {
                "status": "done",
                "should_continue": False,
                "continuation_prompt": None,
                "message": f"✓ Goal achieved ({cl_done}/{cl_total} done, {cl_imp} impossible).",
            }

        # 2) Out of turns?
        if state.turns_used >= state.max_turns:
            state.status = "paused"
            state.paused_reason = (
                f"turn budget exhausted ({state.turns_used}/{state.max_turns})"
            )
            save_goal(self.session_id, state)
            return {
                "status": "paused",
                "should_continue": False,
                "continuation_prompt": None,
                "message": (
                    f"⏸ Goal paused — {state.turns_used}/{state.max_turns} turns "
                    "used. Use /goal resume to keep going, or /goal clear to stop."
                ),
            }

        # 3) Continue. Build the continuation prompt (which will pick up
        # any pending user challenges).
        save_goal(self.session_id, state)
        cl_total, cl_done, cl_imp, _ = state.checklist_counts()
        progress = ""
        if cl_total:
            progress = f" — {cl_done + cl_imp}/{cl_total} done"
        return {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": self.next_continuation_prompt(),
            "message": (
                f"↻ Continuing toward goal ({state.turns_used}/{state.max_turns}{progress})."
            ),
        }

    # --- continuation prompt ------------------------------------------

    def next_continuation_prompt(self) -> Optional[str]:
        """Build the user-role message to feed into the next turn.

        Three flavors:
        1. No checklist yet → prompt the agent to decompose.
        2. Checklist exists → standard continuation with progress.
        3. Pending user challenges → prepend to the continuation (and
           drain the queue so each challenge is delivered exactly once).
        """
        if not self._state or self._state.status != "active":
            return None

        if not self._state.checklist:
            base = DECOMPOSE_PROMPT_TEMPLATE.format(goal=self._state.goal)
        else:
            base = CONTINUATION_PROMPT_WITH_CHECKLIST_TEMPLATE.format(
                goal=self._state.goal,
                done=sum(
                    1 for it in self._state.checklist
                    if it.status in TERMINAL_ITEM_STATUSES
                ),
                total=len(self._state.checklist),
                checklist=self._state.render_checklist(numbered=False),
            )

        if self._state.pending_challenges:
            challenges = "\n\n".join(self._state.pending_challenges)
            self._state.pending_challenges = []
            save_goal(self.session_id, self._state)
            return f"{challenges}\n\n{base}"
        return base


__all__ = [
    "ChecklistItem",
    "GoalState",
    "GoalManager",
    "DEFAULT_MAX_TURNS",
    "ITEM_PENDING",
    "ITEM_COMPLETED",
    "ITEM_IMPOSSIBLE",
    "ITEM_MARKERS",
    "TERMINAL_ITEM_STATUSES",
    "VALID_ITEM_STATUSES",
    "ADDED_BY_AGENT",
    "ADDED_BY_USER",
    "DECOMPOSE_PROMPT_TEMPLATE",
    "CONTINUATION_PROMPT_TEMPLATE",
    "CONTINUATION_PROMPT_WITH_CHECKLIST_TEMPLATE",
    "CONTINUATION_PROMPT_NO_CHECKLIST_TEMPLATE",
    "CHALLENGE_PROMPT_TEMPLATE",
    "load_goal",
    "save_goal",
    "clear_goal",
]
