"""Trust engine — rule-based approval/denial for tool invocations.

Inspired by Vellum Assistant's trust rules v3 schema.  Sits BEFORE the
existing pattern-based dangerous-command detection and the yolo bypass:

    tool invocation → evaluate_trust() → decision
      ├── deny rule matched → blocked (regardless of yolo)
      ├── allow rule matched → bypass prompt (subject to hardline floor)
      ├── ask rule matched → always prompt
      └── no match → fall through to existing check_dangerous_command

The trust engine is **opt-in**.  If ``~/.hermes/trust.json`` doesn't exist
and the config doesn't define any rules, every call returns ``"no_match"``
and the existing flow is unchanged.

Rule shape (stored as JSON list)::

    {
      "id": "allow-readonly-git",
      "tool": "terminal",
      "pattern": "git status*",
      "scope": "everywhere",
      "decision": "allow",
      "priority": 100
    }

- ``tool``: tool name (``terminal``, ``file_write``, ``file_read``, ...).
  ``*`` matches any tool.
- ``pattern``: fnmatch glob against the candidate string.  Missing = ``*``.
- ``scope``: ``everywhere`` (default) or a filesystem path prefix.  Only
  enforced for file tools where the candidate includes a path.
- ``decision``: ``allow`` | ``deny`` | ``ask``.
- ``priority``: integer, higher wins.  Denies beat allows on ties.

Risk classification uses the same dangerous-command detector already in
``tools/approval.py`` — we don't duplicate it, just interpret its output.

Threshold semantics (``approvals.auto_approve_up_to`` in config.yaml)::

    none   — every flagged command prompts (default for cron)
    low    — low-risk auto-allowed; medium/high prompt   (default)
    medium — low+medium auto-allowed; high prompts
    high   — everything auto-allowed
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_RULES_FILENAME = "trust.json"

# Valid rule decisions — parsed at load time, invalid rules are dropped with a warning.
_VALID_DECISIONS = frozenset({"allow", "deny", "ask"})

# Threshold levels (ordered ascending so we can compare via index).
_THRESHOLDS = ("none", "low", "medium", "high")
_RISK_LEVELS = ("low", "medium", "high")


@dataclass
class TrustRule:
    """One entry in ``trust.json``.

    ``scope`` / ``priority`` are optional with sensible defaults.  Missing
    optional fields on stored rules are filled in at load time.
    """

    id: str
    tool: str = "*"
    pattern: str = "*"
    scope: str = "everywhere"
    decision: Literal["allow", "deny", "ask"] = "allow"
    priority: int = 50

    def matches(self, *, tool: str, candidate: str, path: Optional[str] = None) -> bool:
        """Does this rule apply to the given tool+candidate (+optional path)?

        Matching is conservative: the tool must match (or the rule's tool is
        ``*``), the candidate must match the pattern, and if ``scope`` is a
        filesystem prefix the ``path`` argument must start with it.
        """
        if self.tool not in ("*", tool):
            return False
        if not fnmatch.fnmatchcase(candidate, self.pattern):
            # Fallback to case-insensitive match — users frequently write
            # "Git Push" style patterns.
            if not fnmatch.fnmatch(candidate.lower(), self.pattern.lower()):
                return False
        if self.scope and self.scope != "everywhere" and path:
            try:
                # Normalize both sides so "./foo" / "foo" / "/abs/foo" compare sanely.
                if not os.path.abspath(path).startswith(os.path.abspath(self.scope)):
                    return False
            except (TypeError, ValueError):
                return False
        return True


@dataclass
class TrustDecision:
    """The outcome of a single ``evaluate_trust()`` call."""

    decision: Literal["allow", "deny", "ask", "no_match"]
    rule_id: Optional[str] = None
    reason: str = ""
    risk: Literal["low", "medium", "high", "unknown"] = "unknown"
    matched: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _rules_path() -> Path:
    return get_hermes_home() / _RULES_FILENAME


def load_rules() -> List[TrustRule]:
    """Read ``trust.json`` and return a list of valid rules.

    Silently tolerates a missing file (returns empty list). Logs a warning and
    drops rules that don't parse — the engine should never crash user tooling
    over a malformed file.
    """
    path = _rules_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("trust.json parse error: %s; treating as empty", e)
        return []
    if not isinstance(raw, list):
        logger.warning("trust.json must be a JSON array; got %s", type(raw).__name__)
        return []

    rules: List[TrustRule] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning("trust.json rule #%d is not an object; skipping", i)
            continue
        try:
            decision = str(entry.get("decision", "allow")).lower()
            if decision not in _VALID_DECISIONS:
                logger.warning(
                    "trust.json rule %r has invalid decision %r; skipping",
                    entry.get("id"), decision,
                )
                continue
            rule = TrustRule(
                id=str(entry.get("id") or f"rule-{i}"),
                tool=str(entry.get("tool", "*")) or "*",
                pattern=str(entry.get("pattern", "*")) or "*",
                scope=str(entry.get("scope", "everywhere")) or "everywhere",
                decision=decision,  # type: ignore[arg-type]
                priority=int(entry.get("priority", 50)),
            )
            rules.append(rule)
        except (ValueError, TypeError) as e:
            logger.warning("trust.json rule %r malformed: %s; skipping",
                           entry.get("id"), e)
    return rules


def save_rules(rules: List[TrustRule]) -> None:
    path = _rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps([asdict(r) for r in rules], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    from utils import atomic_replace
    atomic_replace(tmp, path)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _find_matching_rules(
    rules: List[TrustRule], *, tool: str, candidate: str, path: Optional[str]
) -> List[TrustRule]:
    return [r for r in rules if r.matches(tool=tool, candidate=candidate, path=path)]


def _pick_winning_rule(matched: List[TrustRule]) -> Optional[TrustRule]:
    """Highest priority wins; on ties, deny beats ask beats allow."""
    if not matched:
        return None
    # Sort so the winner is first: by -priority, then deny<ask<allow order.
    decision_order = {"deny": 0, "ask": 1, "allow": 2}
    matched_sorted = sorted(
        matched,
        key=lambda r: (-int(r.priority), decision_order.get(r.decision, 99)),
    )
    return matched_sorted[0]


def classify_risk(tool: str, candidate: str) -> str:
    """Return ``"low" | "medium" | "high" | "unknown"`` for a tool invocation.

    Reuses ``tools/approval.detect_dangerous_command`` for shell commands so
    there is one source of truth for "is this shell action dangerous".  Other
    tools get a simple heuristic:

    - ``file_read`` / ``read_file`` / ``search_files`` / ``web_search`` / ``web_extract``
      / ``browser_*`` nav → low (read-only / informational)
    - ``file_write`` / ``patch`` / ``write_file`` → medium
    - Anything else → unknown (treated as medium by the threshold gate)
    """
    tool_key = (tool or "").lower()

    if tool_key in ("terminal", "bash", "shell", "host_bash"):
        try:
            from tools.approval import detect_dangerous_command, detect_hardline_command

            is_hard, _ = detect_hardline_command(candidate)
            if is_hard:
                return "high"
            is_dangerous, _, _ = detect_dangerous_command(candidate)
            return "high" if is_dangerous else "low"
        except Exception:
            # If the existing detector can't be imported for any reason,
            # assume medium so we don't silently allow bad commands.
            return "medium"

    if tool_key in (
        "file_read", "read_file", "search_files", "glob", "grep",
        "list_directory", "web_search", "web_extract", "web_fetch",
    ):
        return "low"
    if tool_key.startswith("browser_") and "navigate" in tool_key:
        return "low"
    if tool_key in ("file_write", "write_file", "patch", "file_edit", "host_file_write"):
        return "medium"

    return "unknown"


def _threshold_allows(risk: str, threshold: str) -> bool:
    """Is ``risk`` at or below ``threshold``?"""
    if threshold not in _THRESHOLDS:
        threshold = "low"
    if risk not in _RISK_LEVELS:
        # Unknown risk: treat as medium for threshold purposes.
        risk = "medium"
    return _RISK_LEVELS.index(risk) <= _THRESHOLDS.index(threshold) - 1


def _read_threshold() -> str:
    """Resolve the ``auto_approve_up_to`` threshold from config.yaml (default 'low')."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        approvals = cfg.get("approvals", {}) if isinstance(cfg, dict) else {}
        threshold = str(approvals.get("auto_approve_up_to", "low")).lower()
    except Exception:
        return "low"
    return threshold if threshold in _THRESHOLDS else "low"


def evaluate_trust(
    *,
    tool: str,
    candidate: str,
    path: Optional[str] = None,
    rules: Optional[List[TrustRule]] = None,
    threshold: Optional[str] = None,
) -> TrustDecision:
    """Evaluate tool+candidate against the configured trust rules.

    ``candidate`` is the rendered string to match against rule patterns
    (typically the shell command for ``terminal``, or the file path for file
    tools).  ``path`` is an optional filesystem path used for the ``scope``
    check; for ``terminal`` commands callers can leave it ``None``.

    Return values:

    - ``decision == "allow"`` / ``"deny"`` / ``"ask"``: a rule matched. The
      caller MUST honor the decision.  ``allow`` and ``ask`` are still
      subject to the hardline floor in ``tools/approval.py`` — deny rules
      in ``trust.json`` cannot grant permission to run ``rm -rf /``.
    - ``decision == "no_match"``: no rule applied; the caller should fall
      through to its existing approval logic.  The ``risk`` field is still
      populated so callers can make threshold-based decisions themselves.
    """
    rules = rules if rules is not None else load_rules()
    risk = classify_risk(tool, candidate)

    matched = _find_matching_rules(rules, tool=tool, candidate=candidate, path=path)
    winner = _pick_winning_rule(matched)

    if winner is not None:
        return TrustDecision(
            decision=winner.decision,
            rule_id=winner.id,
            reason=f"rule {winner.id!r} (priority {winner.priority}) matched {tool}:{candidate!r}",
            risk=risk,  # type: ignore[arg-type]
            matched=[r.id for r in matched],
        )

    return TrustDecision(
        decision="no_match",
        rule_id=None,
        reason="no rule matched",
        risk=risk,  # type: ignore[arg-type]
        matched=[],
    )


def explain(tool: str, candidate: str, path: Optional[str] = None) -> Dict[str, object]:
    """Return a full explain payload — every matched rule plus threshold / risk.

    Used by ``hermes trust why`` and by debug logging.
    """
    rules = load_rules()
    matched = _find_matching_rules(rules, tool=tool, candidate=candidate, path=path)
    winner = _pick_winning_rule(matched)
    threshold = _read_threshold()
    risk = classify_risk(tool, candidate)
    return {
        "tool": tool,
        "candidate": candidate,
        "path": path,
        "risk": risk,
        "threshold": threshold,
        "threshold_allows_risk": _threshold_allows(risk, threshold) if risk in _RISK_LEVELS else False,
        "matched_rules": [asdict(r) for r in matched],
        "winning_rule": (asdict(winner) if winner else None),
        "rule_count": len(rules),
    }
