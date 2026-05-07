"""hermes trust — manage trust rules for tool invocations.

Subcommands:

    hermes trust list                           # show all rules
    hermes trust add --tool terminal --pattern 'git status*' --decision allow
    hermes trust remove <rule-id>
    hermes trust show <rule-id>                 # print one rule's full body
    hermes trust why --tool <t> --cmd '<c>'     # explain: what would happen?
    hermes trust init                           # seed a sensible starter bundle

All rules persist to ~/.hermes/trust.json.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import List

from hermes_constants import display_hermes_home
from tools.trust import TrustRule, explain, load_rules, save_rules


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def trust_command(args) -> None:
    sub = getattr(args, "trust_action", None)

    if not sub:
        print("Usage: hermes trust {list|add|remove|show|why|init}")
        print("Run 'hermes trust --help' for details.")
        return

    if sub in ("list", "ls"):
        _cmd_list(args)
    elif sub == "add":
        _cmd_add(args)
    elif sub in ("remove", "rm"):
        _cmd_remove(args)
    elif sub == "show":
        _cmd_show(args)
    elif sub == "why":
        _cmd_why(args)
    elif sub == "init":
        _cmd_init(args)
    else:
        print(f"Unknown trust subcommand: {sub}")


def _cmd_list(args) -> None:
    rules = load_rules()
    if not rules:
        print("No trust rules configured.")
        print()
        print(f"File:   {display_hermes_home()}/trust.json")
        print("Add one with:")
        print("  hermes trust add --tool terminal --pattern 'git status*' --decision allow")
        return

    print(f"{'ID':<28} {'TOOL':<14} {'DECISION':<8} {'PRIO':<5} PATTERN")
    for rule in sorted(rules, key=lambda r: (-r.priority, r.id)):
        print(
            f"{rule.id:<28} {rule.tool:<14} {rule.decision:<8} {rule.priority:<5} "
            f"{rule.pattern}"
        )


def _cmd_add(args) -> None:
    rule_id = (args.id or "").strip().lower()
    if not rule_id:
        rule_id = f"rule-{uuid.uuid4().hex[:8]}"
    if not _ID_RE.match(rule_id):
        print(f"Error: id must be lowercase alphanumerics + '-'/'_' (got {args.id!r})")
        return

    if args.decision not in ("allow", "deny", "ask"):
        print(f"Error: --decision must be allow/deny/ask (got {args.decision!r})")
        return

    rules = load_rules()
    if any(r.id == rule_id for r in rules):
        print(f"Error: a rule with id '{rule_id}' already exists. Remove it first or pick another --id.")
        return

    new_rule = TrustRule(
        id=rule_id,
        tool=args.tool or "*",
        pattern=args.pattern or "*",
        scope=args.scope or "everywhere",
        decision=args.decision,
        priority=int(args.priority),
    )
    rules.append(new_rule)
    save_rules(rules)

    print(f"Added rule '{rule_id}':")
    print(json.dumps(new_rule.__dict__, indent=2))


def _cmd_remove(args) -> None:
    rule_id = args.id.strip().lower()
    rules = load_rules()
    kept = [r for r in rules if r.id != rule_id]
    if len(kept) == len(rules):
        print(f"No rule with id '{rule_id}' — nothing removed.")
        return
    save_rules(kept)
    print(f"Removed rule '{rule_id}'.")


def _cmd_show(args) -> None:
    rule_id = args.id.strip().lower()
    for rule in load_rules():
        if rule.id == rule_id:
            print(json.dumps(rule.__dict__, indent=2))
            return
    print(f"No rule with id '{rule_id}'.")


def _cmd_why(args) -> None:
    payload = explain(args.tool, args.cmd)
    print(json.dumps(payload, indent=2))

    # A readable summary under the JSON.
    print()
    print("Decision:")
    winner = payload.get("winning_rule")
    if winner:
        print(
            f"  ➜ {winner['decision'].upper()} via rule '{winner['id']}' "
            f"(priority {winner['priority']}, pattern {winner['pattern']!r})"
        )
    else:
        risk = payload.get("risk")
        thr = payload.get("threshold")
        allowed = payload.get("threshold_allows_risk")
        print(
            f"  ➜ no rule matched; risk={risk}, threshold={thr} → "
            f"{'auto-approved' if allowed else 'prompts'}"
        )


def _cmd_init(args) -> None:
    """Seed a sensible starter bundle of read-only allow rules.

    Intentionally minimal — users should review before relying on it.
    Refuses to overwrite an existing trust.json.
    """
    existing = load_rules()
    if existing and not getattr(args, "force", False):
        print(
            f"Refusing to overwrite existing trust rules. Re-run with --force "
            f"or inspect {display_hermes_home()}/trust.json first."
        )
        return

    starter: List[TrustRule] = [
        TrustRule(id="starter-allow-git-status", tool="terminal",
                  pattern="git status*", decision="allow", priority=50),
        TrustRule(id="starter-allow-git-log", tool="terminal",
                  pattern="git log*", decision="allow", priority=50),
        TrustRule(id="starter-allow-git-diff", tool="terminal",
                  pattern="git diff*", decision="allow", priority=50),
        TrustRule(id="starter-allow-ls", tool="terminal",
                  pattern="ls*", decision="allow", priority=50),
        TrustRule(id="starter-allow-cat-readonly", tool="terminal",
                  pattern="cat *", decision="allow", priority=50),
        TrustRule(id="starter-allow-file-read", tool="file_read",
                  pattern="*", decision="allow", priority=50),
        TrustRule(id="starter-allow-search-files", tool="search_files",
                  pattern="*", decision="allow", priority=50),
    ]
    save_rules(starter)
    print(f"Seeded {len(starter)} starter rule(s) to {display_hermes_home()}/trust.json.")
    print("Inspect with 'hermes trust list'; remove any you don't want.")
