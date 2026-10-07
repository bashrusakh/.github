#!/usr/bin/env python3
"""Deterministic, no-LLM validator for the reusable triage masters in this repository.

Validates each master's source (``.github/workflows/<id>.md``) and generated
(``.github/workflows/<id>.lock.yml``) artifact against the published capability contract of
the central triage deployment:

* the workflow is reusable (``on: workflow_call``) and silent (``status-comment: false``);
* the shared prompt imports are exactly the expected ones, all at the single audited pin;
* the agent cannot reach a shell, the CLI proxy, the editor, the repository files, or a diff;
* the GitHub MCP surface is exactly ``issues`` toolset, this repository, ``min-integrity: none``,
  and the narrow allowed tool list — no forbidden GitHub tool grant in either the manifest
  comment form or the shell-quoted runtime command form (see ``allow_tool_grants``);
* the safe-output surface is exactly the label add/remove pair with the expected allow/block
  lists and no comment tool;
* the masters carry no policy contract of their own: the ``contract_files`` default must name
  caller-owned files, never a file this repository ships.

``--allow-tool`` grant coverage: a compiled lock states the agent's tool grants twice, and
both statements are parsed and checked against the same forbidden set:

* the *manifest comment* form ``# --allow-tool github(<tool>)`` in the engine-arguments
  comment block near ``arguments (sorted):``; and
* the *runtime command* form actually executed by the agent job, where gh-aw
  shell-escapes the quoting: ``-- /bin/bash -c '... --allow-tool '\\''github(<tool>)'\\'' ...'``.
  The lock text is normalized first — the sandwich sequence ``'\\''`` (close quote,
  escaped quote, reopen quote) is collapsed to a single ``'`` — so the runtime grants are
  read from the real executed command string, not from a comment.

Beyond the two grant forms above, the lock must also never gain a runtime tool grant that
would let the agent run shell or reach every tool: a runtime ``--allow-tool shell`` (quoted
or bare) or ``--allow-all-tools`` anywhere in the lock is an error.

The validator is fail-closed: any unparsable input, missing expected key, unexpected value or
unexpected extra capability is an error, and the process exits non-zero. It has no network
access, no LLM, and no third-party dependencies.

Usage:
    python3 .github/aw/validate-masters.py [--root DIR] [--master ID]...

Exit codes: 0 = all checks passed, 1 = at least one check failed, 2 = the validator could not
complete (unparsable input), both fail closed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------------------------
# Expected capability contract
# --------------------------------------------------------------------------------------------

#: The single audited pin of the shared gh-aw triage prompt package
#: (bashrusakh/repo-docs-sync, packages/ghaw-triage/).
SHARED_PIN = "e2de4a989077b7fdf558ef5656040dc2e547e674"
SHARED_REPO = "bashrusakh/repo-docs-sync"
SHARED_DIR = "packages/ghaw-triage/workflows"

GH_AW_VERSION = "v0.91.4"
MCP_GATEWAY_IMAGE = "ghcr.io/github/gh-aw-mcpg:v0.4.29"
MCP_GATEWAY_DIGEST = "sha256:ec08867ac8a4823e01efb2de2ba85a313199bb7ae666ef70ae58effc162a9bf3"

#: Vendored import cache location, mirroring gh-aw's own layout.
VENDOR_ROOT = ".github/aw/imports"

#: Safe-output tools gh-aw emits for every master here, in manifest order.
EXPECTED_MANIFEST_SAFE_OUTPUTS = [
    "add_labels",
    "missing_data",
    "missing_tool",
    "noop",
    "remove_labels",
    "report_incomplete",
]

#: The only GitHub MCP tools any master may expose to the agent.
EXPECTED_GITHUB_TOOLS = ["issue_read", "search_issues"]

#: The complete ``--allow-tool`` token set the compiled agent command may carry, in both the
#: manifest comment block and the executed runtime command. ``safeoutputs`` is the safe-output
#: MCP server, not a shell.
EXPECTED_GRANT_TOKENS = sorted(
    [f"github({name})" for name in EXPECTED_GITHUB_TOOLS] + ["safeoutputs"]
)

#: GitHub MCP tools that would expose file contents, patches, diffs or a PR search surface.
#: None may appear as a grant (in an allow list, a toolset, or a lock ``--allow-tool``).
FORBIDDEN_GITHUB_GRANTS = [
    "pull_request_read",
    "get_pull_request_files",
    "get_pull_request_diff",
    "get_files",
    "get_file_contents",
    "search_code",
    "list_pull_requests",
    "search_pull_requests",
]

#: Blocked-label patterns required in every add/remove label list (source and lock).
EXPECTED_BLOCKED = [
    "priority-*",
    "codex-*",
    "confirmed",
    "invalid",
    "wontfix",
    "good first issue",
    "help wanted",
    "~*",
    "*[bot]",
]

#: Safe-output keys that would widen the mutation surface beyond managed labels.
FORBIDDEN_SAFE_OUTPUTS = [
    "add_comment",
    "add-comment",
    "create_issue",
    "update_issue",
    "close_issue",
    "create_pull_request",
    "update_pull_request",
    "push_to_pull_request_branch",
    "create_discussion",
    "create_report_incomplete_issue",
    "create_missing_tool_issue",
    "create_missing_data_issue",
    "close_discussion",
    "assign_to_user",
    "assign_to_agent",
    "create_or_update_file",
    "create_report_incomplete_issue_comment",
    "get_secret",
]

#: Safe-output keys gh-aw emits alongside the declared label tools. ``report_incomplete`` is
#: a diagnostic signal only; the issue-creation handler is deliberately excluded below.
BUILTIN_SAFE_OUTPUTS = [
    "missing_data",
    "missing_tool",
    "noop",
    "report_incomplete",
]

#: The complete expected safe-output tool set of every master's compiled lock.
EXPECTED_LOCK_SAFE_OUTPUTS = sorted(
    {"add_labels", "remove_labels"} | set(BUILTIN_SAFE_OUTPUTS)
)

#: Consumer-repository contract files. This repository is a reusable-master host, so it must
#: not ship any of them: a master's contract comes from the *caller*, resolved at run time by
#: the pre-agent policy step.
CONSUMER_CONTRACT_FILES = ["triage-policy.md", "AGENTS.md", "CONTRIBUTING.md"]

#: Files a caller may legitimately declare in ``contract_files``; used only to prove that the
#: defaults are caller-owned names and not master-side artifacts.
CALLER_CONTRACT_SUFFIXES = [".md", ".yml", ".yaml", ".toml", ".txt"]

MASTER_IDS = ["triage-issue", "triage-pr", "triage-backlog"]

#: Per-master expectations. ``imports`` is order-sensitive and exact.
#:
#: ``policy_step_precreates_parent`` asserts the policy step creates the redirect target's
#: parent directory BEFORE it opens ``$dir/$f.tmp``. Without that ordering, a nested contract
#: path (``.github/triage-policy.md``, the first entry of every default) always fails, because
#: the shell cannot open the redirect target in a directory that does not exist yet, and the
#: step then reports the file as missing and fails closed.
#:
#: All three masters assert this. ``triage-issue``'s ordering fix (``mkdir -p`` before the
#: redirect) landed on ``main`` and is present in this branch's merge result, so the validator
#: pins the corrected shape for it too rather than exempting it.
MASTERS = {
    "triage-issue": {
        "imports": [
            f"{SHARED_REPO}/{SHARED_DIR}/contract-invariant.md@{SHARED_PIN}",
            f"{SHARED_REPO}/{SHARED_DIR}/issue-triage-core.md@{SHARED_PIN}",
        ],
        "permissions": {"contents": "read", "issues": "read"},
        "contract_files": ".github/triage-policy.md AGENTS.md CONTRIBUTING.md",
        "max_ai_credits": 5,
        "github_allowed": [("issue_read", 8), ("search_issues", 3)],
        "add_labels": (3, False),
        "max_labels_per_add": 3,
        "remove_labels": (3, False),
        "pre_agent_steps": ["Resolve repository policy contract at the trusted Policy SHA"],
        "policy_step_precreates_parent": True,
    },
    "triage-pr": {
        "imports": [
            f"{SHARED_REPO}/{SHARED_DIR}/contract-invariant.md@{SHARED_PIN}",
            f"{SHARED_REPO}/{SHARED_DIR}/pr-intake-core.md@{SHARED_PIN}",
        ],
        "permissions": {"contents": "read", "pull-requests": "read", "issues": "read"},
        "contract_files": (
            ".github/triage-policy.md AGENTS.md CONTRIBUTING.md .github/PULL_REQUEST_TEMPLATE.md"
        ),
        "max_ai_credits": 8,
        "github_allowed": [("issue_read", 4), ("search_issues", 2)],
        "add_labels": (3, False),
        "max_labels_per_add": 3,
        "remove_labels": (3, False),
        "pre_agent_steps": [
            "Resolve repository policy contract at the trusted Policy SHA",
            "Resolve PR metadata context (filenames only, no diff)",
        ],
        "policy_step_precreates_parent": True,
    },
    "triage-backlog": {
        "imports": [
            f"{SHARED_REPO}/{SHARED_DIR}/contract-invariant.md@{SHARED_PIN}",
            f"{SHARED_REPO}/{SHARED_DIR}/backlog-retriage-core.md@{SHARED_PIN}",
        ],
        "permissions": {"contents": "read", "issues": "read", "pull-requests": "read"},
        "contract_files": (
            ".github/triage-policy.md AGENTS.md CONTRIBUTING.md"
            " .github/ISSUE_TEMPLATE/bug_report.yml"
            " .github/PULL_REQUEST_TEMPLATE.md .github/labels.yml"
        ),
        "max_ai_credits": 10,
        "github_allowed": [("issue_read", 12), ("search_issues", 3)],
        "add_labels": (5, False),
        "max_labels_per_add": 5,
        "remove_labels": (5, False),
        "pre_agent_steps": [
            "Resolve repository policy contract at the trusted Policy SHA",
            "Resolve bounded backlog batch",
        ],
        "policy_step_precreates_parent": True,
    },
}

#: Managed label families. ``add`` order is the master's declared order.
LABELS_ADD = {
    "triage-issue": ["bug", "enhancement", "documentation", "question", "refactor", "ci", "needs-info", "duplicate"],
    "triage-pr": ["bug", "enhancement", "documentation", "question", "duplicate", "refactor", "ci"],
    "triage-backlog": ["bug", "enhancement", "documentation", "question", "refactor", "ci", "needs-info", "duplicate"],
}
LABELS_REMOVE = {
    "triage-issue": ["bug", "enhancement", "documentation", "question", "refactor", "ci", "needs-info", "duplicate"],
    "triage-pr": ["bug", "enhancement", "documentation", "question", "refactor", "ci", "duplicate"],
    "triage-backlog": ["bug", "enhancement", "documentation", "question", "refactor", "ci", "needs-info", "duplicate"],
}

EXPECTED_ENGINE = {
    "id": "copilot",
    "model": "mimo-v2.6-flash-free",
    "bare": True,
    "args": ["--deny-tool", "shell"],
    "group": "gh-aw-triage-${{ github.repository }}",
    "queue": "max",
}
EXPECTED_NETWORK = ["defaults", "github", "opencode.ai"]
EXPECTED_MAX_TURNS = 20
EXPECTED_TIMEOUT = 20


# --------------------------------------------------------------------------------------------
# Minimal YAML-subset reader (stdlib only)
# --------------------------------------------------------------------------------------------


class ParseError(Exception):
    """Raised when the frontmatter uses a construct this validator cannot read safely."""


_BLOCK_SCALAR = {"|", "|-", "|+", ">", ">-", ">+"}
_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\- ]*):(?:\s+(.*))?$")


def _strip(lines: list[str]) -> list[tuple[int, str]]:
    """Drop blank lines and full-line comments, returning ``(indent, text)`` pairs."""
    out: list[tuple[int, str]] = []
    for raw in lines:
        expanded = raw.replace("\t", "    ")
        stripped = expanded.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(expanded) - len(expanded.lstrip(" "))
        out.append((indent, expanded.strip()))
    return out


def _scalar(text: str):
    """Convert a YAML flow scalar / sequence to a Python value."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("["):
        if not text.endswith("]"):
            raise ParseError(f"unterminated flow sequence: {text!r}")
        body = text[1:-1].strip()
        if not body:
            return []
        parts, depth, current, quote = [], 0, "", None
        for ch in body:
            if quote:
                current += ch
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
                current += ch
            elif ch in "[{":
                depth += 1
                current += ch
            elif ch in "]}":
                depth -= 1
                current += ch
            elif ch == "," and depth == 0:
                parts.append(current)
                current = ""
            else:
                current += ch
        parts.append(current)
        return [_scalar(p) for p in parts]
    if (text.startswith('"') and text.endswith('"') and len(text) > 1) or (
        text.startswith("'") and text.endswith("'") and len(text) > 1
    ):
        if text[0] == '"':
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:  # pragma: no cover - defensive
                raise ParseError(f"bad double-quoted scalar {text!r}: {exc}") from exc
        return text[1:-1].replace("''", "'")
    # YAML core scalars: coerce so that typed comparisons are meaningful.
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"[+-]?[0-9]+", text):
        return int(text)
    if re.fullmatch(r"[+-]?(?:[0-9]*\.[0-9]+|[0-9]+\.[0-9]*)(?:[eE][+-]?[0-9]+)?", text):
        return float(text)
    return text


def _parse_node(lines: list[tuple[int, str]], i: int, indent: int):
    """Parse the node at ``lines[i]`` whose block is indented by ``indent``."""
    node = None
    is_list = False
    while i < len(lines):
        ind, text = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise ParseError(f"unexpected indent {ind} (expected {indent}) at {text!r}")
        if text == "-" or text.startswith("- "):
            if node is None:
                node, is_list = [], True
            if not is_list:
                raise ParseError(f"list item inside mapping at {text!r}")
            rest = text[2:].strip() if text.startswith("- ") else ""
            if rest and _KEY_RE.match(rest):
                # A mapping item: re-read the remainder as a nested mapping block.
                lines[i] = (indent + 2, rest)
                value, i = _parse_node(lines, i, indent + 2)
                node.append(value)
            elif rest:
                node.append(_scalar(rest))
                i += 1
            else:
                value, i = _parse_node(lines, i + 1, indent + 2)
                node.append(value)
            continue
        match = _KEY_RE.match(text)
        if not match:
            raise ParseError(f"unsupported frontmatter line at indent {indent}: {text!r}")
        if node is None:
            node, is_list = {}, False
        if is_list:
            raise ParseError(f"mapping key inside list at {text!r}")
        key, rest = match.group(1), match.group(2)
        if key in node:
            raise ParseError(f"duplicate key {key!r} at indent {indent}")
        if rest is not None and rest.strip() in _BLOCK_SCALAR:
            body: list[str] = []
            j = i + 1
            while j < len(lines) and lines[j][0] > indent:
                body.append(lines[j][1])
                j += 1
            node[key] = "\n".join(body)
            i = j
            continue
        if rest is None or rest.strip() == "":
            if i + 1 < len(lines) and lines[i + 1][0] > indent:
                value, i = _parse_node(lines, i + 1, lines[i + 1][0])
                node[key] = value
            else:
                node[key] = None
                i += 1
            continue
        node[key] = _scalar(rest)
        i += 1
    if node is None:
        raise ParseError(f"empty node at indent {indent}")
    return node, i


def parse_frontmatter(text: str, origin: str) -> dict:
    """Return the frontmatter mapping of an agentic workflow source file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ParseError(f"{origin}: file does not start with a '---' frontmatter fence")
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
    if end is None:
        raise ParseError(f"{origin}: unterminated frontmatter")
    body = _strip(lines[1:end])
    if not body:
        raise ParseError(f"{origin}: empty frontmatter")
    node, _ = _parse_node(body, 0, body[0][0])
    if not isinstance(node, dict):
        raise ParseError(f"{origin}: frontmatter is not a mapping")
    return node


def dig(node, path: str):
    """Look up a dotted path; returns ``(found, value)``."""
    current = node
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


# --------------------------------------------------------------------------------------------
# Lock helpers
# --------------------------------------------------------------------------------------------


def lock_job_block(lock: str, job: str) -> str | None:
    """Return the text of a top-level job block (``  <job>:`` .. next top-level key)."""
    match = re.search(rf"(?m)^  {re.escape(job)}:\s*$", lock)
    if not match:
        return None
    start = match.start()
    tail = lock[match.end():]
    nxt = re.search(r"(?m)^  [a-z_]+:\s*$", tail)
    end = match.end() + (nxt.start() if nxt else len(tail))
    return lock[start:end]


def lock_yaml_string(lock: str, key: str) -> str | None:
    """Extract a double-quoted YAML scalar assigned to ``key`` on its own line."""
    match = re.search(rf'(?m)^\s+{re.escape(key)}: "(.*)"$', lock)
    return match.group(1) if match else None


def parse_escaped_json(raw: str, origin: str):
    """Parse a JSON document embedded in a YAML double-quoted scalar."""
    candidate = raw.replace('\\"', '"').replace("\\\\", "\\")
    return json.loads(candidate)


#: gh-aw emits the agent command through ``/bin/bash -c '<script>'``, so a single quote inside
#: the script appears as the sandwich ``'\''`` (close quote, backslash-escaped quote, reopen
#: quote). Collapsing it recovers the exact string the shell hands to the CLI.
_ESCAPED_QUOTE = "'\\''"


def normalize_shell_quoting(lock: str) -> str:
    """Collapse gh-aw's shell-escaped quote sandwiches (``'\\''`` -> ``'``).

    After this the runtime ``-- /bin/bash -c '...'`` argument reads as the command string the
    agent job actually executes, so a grant written there is visible to the same regexes used
    on the manifest comments.
    """
    return lock.replace(_ESCAPED_QUOTE, "'")


def allow_tool_grants(lock: str) -> list[tuple[str, str]]:
    """Every ``--allow-tool`` grant in the lock as ``(form, token)`` pairs.

    ``form`` is ``"comment"`` when everything before the grant on its line is whitespace and
    ``#`` (a manifest comment) and ``"runtime"`` otherwise — i.e. a grant inside the executed
    ``/bin/bash -c`` command. Both are extracted from the normalized text, so the escaped
    runtime form (``--allow-tool '\\''github(issue_read)'\\''``) yields the same token as the
    comment form. A single- or double-quoted token is unquoted.
    """
    grants: list[tuple[str, str]] = []
    normalized = normalize_shell_quoting(lock)
    pattern = re.compile(r"--allow-tool\s+(?:(['\"])(?P<quoted>[^\s'\"]+)\1|(?P<bare>[^\s'\"]+))")
    for match in pattern.finditer(normalized):
        line_start = normalized.rfind("\n", 0, match.start()) + 1
        prefix = normalized[line_start:match.start()]
        form = "comment" if prefix.strip(" \t#") == "" else "runtime"
        token = match.group("quoted") or match.group("bare")
        grants.append((form, token))
    return grants


def github_grant_names(lock: str) -> set[str]:
    """All GitHub MCP tool names granted to the agent anywhere in the lock.

    Covers the manifest comment form, the escaped runtime command form, the compiled
    ``mcp_servers`` tools array, and the ``GITHUB_TOOLSETS`` environment value.
    """
    names: set[str] = set()
    for _, token in allow_tool_grants(lock):
        match = re.fullmatch(r"github\(([A-Za-z0-9_]+)\)", token)
        if match:
            names.add(match.group(1))
    for match in re.finditer(r'"name":\s*"github",\s*"tools":\s*\[([^\]]*)\]', lock):
        for name in match.group(1).split(","):
            cleaned = name.strip().strip('"')
            if cleaned:
                names.add(cleaned)
    for match in re.finditer(r'"GITHUB_TOOLSETS":\s*"([^"]*)"', lock):
        names.add("toolset:" + match.group(1))
    return names


def runtime_shell_grants(lock: str) -> list[str]:
    """Runtime grants that would hand the agent a shell or every tool.

    Returns the offending ``--allow-tool`` token for a runtime shell grant and/or the literal
    ``--allow-all-tools`` flag. The manifest comments are not considered: a comment cannot
    change what the agent executes, and the compiled command must never carry these.
    """
    offenders: list[str] = []
    for form, token in allow_tool_grants(lock):
        if form == "runtime" and token == "shell":
            offenders.append("--allow-tool shell")
    if "--allow-all-tools" in lock:
        offenders.append("--allow-all-tools")
    return offenders


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.passed = 0

    def check(self, condition: bool, message: str) -> bool:
        if condition:
            self.passed += 1
        else:
            self.errors.append(message)
        return bool(condition)

    def fail(self, message: str) -> None:
        self.errors.append(message)


def validate_master(root: Path, master: str, report: Report) -> None:
    spec = MASTERS[master]
    label = f"[{master}]"

    source_path = root / ".github" / "workflows" / f"{master}.md"
    lock_path = root / ".github" / "workflows" / f"{master}.lock.yml"

    if not report.check(source_path.is_file(), f"{label} missing source {source_path.name}"):
        return
    if not report.check(lock_path.is_file(), f"{label} missing generated {lock_path.name}"):
        return

    source_text = source_path.read_text(encoding="utf-8")
    lock_text = lock_path.read_text(encoding="utf-8")

    try:
        fm = parse_frontmatter(source_text, source_path.name)
    except ParseError as exc:
        report.fail(f"{label} unparsable frontmatter: {exc}")
        return

    # ---- reusability + silence (source) ----------------------------------------------------
    found, on_block = dig(fm, "on")
    report.check(found and isinstance(on_block, dict), f"{label} source has no 'on:' mapping")
    if isinstance(on_block, dict):
        report.check(
            isinstance(on_block.get("workflow_call"), dict),
            f"{label} source 'on:' must declare workflow_call",
        )
        report.check(
            on_block.get("status-comment") is False,
            f"{label} source must set 'on.status-comment: false'",
        )
        stray = sorted(k for k in on_block if k not in ("workflow_call", "status-comment"))
        report.check(not stray, f"{label} source 'on:' carries unexpected trigger keys {stray}")

    # ---- declared permissions -------------------------------------------------------------
    found, perms = dig(fm, "permissions")
    report.check(
        found and perms == spec["permissions"],
        f"{label} source permissions must be exactly {spec['permissions']}, got {perms}",
    )

    # ---- shared imports at the audited pin -----------------------------------------------
    found, imports = dig(fm, "imports")
    report.check(
        found and imports == spec["imports"],
        f"{label} source imports must be exactly {spec['imports']}, got {imports}",
    )
    report.check(dig(fm, "inlined-imports") == (True, True), f"{label} must set inlined-imports: true")
    if isinstance(imports, list):
        for entry in imports:
            report.check(
                isinstance(entry, str) and entry.endswith("@" + SHARED_PIN),
                f"{label} import {entry!r} is not pinned to {SHARED_PIN}",
            )

    # ---- capability controls (source) -----------------------------------------------------
    report.check(dig(fm, "checkout") == (True, False), f"{label} must set checkout: false")
    report.check(dig(fm, "tools.bash") == (True, False), f"{label} must set tools.bash: false")
    report.check(dig(fm, "tools.cli-proxy") == (True, False), f"{label} must set tools.cli-proxy: false")
    report.check(dig(fm, "tools.edit") == (True, False), f"{label} must set tools.edit: false")

    found, toolsets = dig(fm, "tools.github.toolsets")
    report.check(
        found and toolsets == ["issues"],
        f"{label} tools.github.toolsets must be exactly ['issues'], got {toolsets}",
    )
    found, allowed_repos = dig(fm, "tools.github.allowed-repos")
    report.check(
        found and allowed_repos == ["${{ github.repository }}"],
        f"{label} tools.github.allowed-repos must bind to ['${{ github.repository }}'], got {allowed_repos}",
    )
    report.check(
        dig(fm, "tools.github.min-integrity") == (True, "none"),
        f"{label} tools.github.min-integrity must be 'none'",
    )
    report.check(
        dig(fm, "tools.github.mode") == (True, "local"),
        f"{label} tools.github.mode must be 'local'",
    )

    found, allowed = dig(fm, "tools.github.allowed")
    if report.check(found and isinstance(allowed, list), f"{label} tools.github.allowed must be a list"):
        expected_pairs = spec["github_allowed"]
        actual_pairs = []
        for entry in allowed:
            if not isinstance(entry, dict) or "name" not in entry:
                report.fail(f"{label} tools.github.allowed entry is not a named tool: {entry!r}")
                continue
            actual_pairs.append((entry.get("name"), entry.get("max-calls")))
        report.check(
            actual_pairs == expected_pairs,
            f"{label} tools.github.allowed must be exactly {expected_pairs}, got {actual_pairs}",
        )
        for name, _ in actual_pairs:
            report.check(
                name not in FORBIDDEN_GITHUB_GRANTS,
                f"{label} tools.github.allowed grants forbidden GitHub tool {name!r}",
            )

    # ---- engine --------------------------------------------------------------------------
    report.check(
        dig(fm, "engine.id") == (True, EXPECTED_ENGINE["id"]),
        f"{label} engine.id must be {EXPECTED_ENGINE['id']!r}",
    )
    report.check(
        dig(fm, "engine.model") == (True, EXPECTED_ENGINE["model"]),
        f"{label} engine.model must be {EXPECTED_ENGINE['model']!r}",
    )
    report.check(
        dig(fm, "engine.bare") == (True, EXPECTED_ENGINE["bare"]),
        f"{label} engine.bare must be True",
    )
    found, engine_args = dig(fm, "engine.args")
    report.check(
        found and engine_args == EXPECTED_ENGINE["args"],
        f"{label} engine.args must be exactly {EXPECTED_ENGINE['args']}, got {engine_args}",
    )
    report.check(
        dig(fm, "engine.concurrency.group") == (True, EXPECTED_ENGINE["group"]),
        f"{label} engine.concurrency.group must be {EXPECTED_ENGINE['group']!r}",
    )
    report.check(
        dig(fm, "engine.concurrency.queue") == (True, EXPECTED_ENGINE["queue"]),
        f"{label} engine.concurrency.queue must be 'max'",
    )

    # ---- budgets -------------------------------------------------------------------------
    report.check(
        dig(fm, "max-ai-credits") == (True, spec["max_ai_credits"]),
        f"{label} max-ai-credits must be {spec['max_ai_credits']}",
    )
    report.check(
        dig(fm, "max-turns") == (True, EXPECTED_MAX_TURNS),
        f"{label} max-turns must be {EXPECTED_MAX_TURNS}",
    )
    report.check(
        dig(fm, "timeout-minutes") == (True, EXPECTED_TIMEOUT),
        f"{label} timeout-minutes must be {EXPECTED_TIMEOUT}",
    )
    report.check(
        dig(fm, "network.allowed") == (True, EXPECTED_NETWORK),
        f"{label} network.allowed must be exactly {EXPECTED_NETWORK}",
    )

    # ---- safe outputs (source) -----------------------------------------------------------
    report.check(
        dig(fm, "safe-outputs.report-failure-as-issue") == (True, False),
        f"{label} safe-outputs.report-failure-as-issue must be false",
    )
    report.check(
        dig(fm, "safe-outputs.report-failed-jobs") == (True, False),
        f"{label} safe-outputs.report-failed-jobs must be false",
    )
    report.check(
        dig(fm, "safe-outputs.noop.max") == (True, 1),
        f"{label} safe-outputs.noop.max must remain 1",
    )
    report.check(
        dig(fm, "safe-outputs.noop.report-as-issue") == (True, False),
        f"{label} safe-outputs.noop.report-as-issue must be false",
    )
    report.check(
        dig(fm, "safe-outputs.report-incomplete.create-issue") == (True, False),
        f"{label} safe-outputs.report-incomplete.create-issue must be false",
    )
    for signal in ("missing-tool", "missing-data"):
        report.check(
            dig(fm, f"safe-outputs.{signal}.create-issue") == (True, False),
            f"{label} safe-outputs.{signal}.create-issue must be false",
        )
    found, safe_outputs = dig(fm, "safe-outputs")
    if isinstance(safe_outputs, dict):
        for forbidden in ("add-comment", "add_comment", "create-issue", "create-pull-request"):
            report.check(
                forbidden not in safe_outputs,
                f"{label} safe-outputs must not declare {forbidden!r}",
            )

    add_max, add_issue_intent = spec["add_labels"]
    max_labels_per_add = spec["max_labels_per_add"]
    remove_max, _ = spec["remove_labels"]
    report.check(
        dig(fm, "safe-outputs.add-labels.max-labels") == (True, max_labels_per_add),
        f"{label} safe-outputs.add-labels.max-labels must be {max_labels_per_add}",
    )

    for key, expected_labels, expected_max in (
        ("add-labels", LABELS_ADD[master], add_max),
        ("remove-labels", LABELS_REMOVE[master], remove_max),
    ):
        found, block = dig(fm, f"safe-outputs.{key}")
        if not report.check(found and isinstance(block, dict), f"{label} safe-outputs.{key} missing"):
            continue
        report.check(
            block.get("allowed") == expected_labels,
            f"{label} safe-outputs.{key}.allowed must be {expected_labels}, got {block.get('allowed')}",
        )
        report.check(
            block.get("blocked") == EXPECTED_BLOCKED,
            f"{label} safe-outputs.{key}.blocked must be exactly the {len(EXPECTED_BLOCKED)} required patterns, got {block.get('blocked')}",
        )
        report.check(
            block.get("max") == expected_max,
            f"{label} safe-outputs.{key}.max must be {expected_max}, got {block.get('max')}",
        )
    report.check(
        dig(fm, "safe-outputs.add-labels.issue-intent") == (True, add_issue_intent),
        f"{label} safe-outputs.add-labels.issue-intent must be {add_issue_intent}",
    )
    found, remove_block = dig(fm, "safe-outputs.remove-labels")
    if isinstance(remove_block, dict):
        report.check(
            "issue-intent" not in remove_block,
            f"{label} safe-outputs.remove-labels must not declare issue-intent",
        )

    # ---- pre-agent steps -----------------------------------------------------------------
    found, steps = dig(fm, "pre-agent-steps")
    if report.check(found and isinstance(steps, list), f"{label} pre-agent-steps must be a list"):
        names = [step.get("name") for step in steps if isinstance(step, dict)]
        report.check(
            names == spec["pre_agent_steps"],
            f"{label} pre-agent-steps must be exactly {spec['pre_agent_steps']}, got {names}",
        )

    # ---- contract provenance -------------------------------------------------------------
    found, contract_files = dig(fm, "contract_files")
    if not found:
        found, contract_files = dig(fm, "on.workflow_call.inputs.contract_files.default")
    report.check(
        contract_files == spec["contract_files"],
        f"{label} contract_files default must be {spec['contract_files']!r}, got {contract_files!r}",
    )
    if isinstance(contract_files, str):
        entries = contract_files.split()
        report.check(bool(entries), f"{label} contract_files default must not be empty")
        for entry in entries:
            report.check(
                not entry.startswith("/") and ".." not in entry.split("/"),
                f"{label} contract_files entry {entry!r} must be a caller-relative path",
            )
            report.check(
                any(entry.endswith(suffix) for suffix in CALLER_CONTRACT_SUFFIXES),
                f"{label} contract_files entry {entry!r} must name a caller contract document",
            )
            # A master carries no contract of its own: its defaults must never name a file
            # this repository ships, or the master would be validating the master.
            report.check(
                not (root / entry).exists(),
                f"{label} contract_files entry {entry!r} resolves to a master-side file; "
                "a master must not name a contract that lives in itself",
            )
            report.check(
                not entry.startswith(".github/workflows/"),
                f"{label} contract_files entry {entry!r} names a workflow file, not a caller contract",
            )
        found_cf, cf_input = dig(fm, "on.workflow_call.inputs.contract_files")
        report.check(
            isinstance(cf_input, dict),
            f"{label} on.workflow_call.inputs must declare contract_files",
        )
        report.check(
            "CONTRACT_FILES: ${{ inputs.contract_files }}" in source_text,
            f"{label} pre-agent policy step must read ${{ inputs.contract_files }}",
        )
        # Ordering assertion: the policy step must create the parent directory of each
        # contract file before the `> "$dir/$f.tmp"` redirect opens it, or a nested contract
        # path (the first entry of every default) can never be fetched.
        if spec.get("policy_step_precreates_parent"):
            _, raw_steps = dig(fm, "pre-agent-steps")
            policy_step = next(
                (
                    s
                    for s in (raw_steps if isinstance(raw_steps, list) else [])
                    if isinstance(s, dict)
                    and s.get("name") == "Resolve repository policy contract at the trusted Policy SHA"
                ),
                None,
            )
            body = (policy_step or {}).get("run", "") if isinstance(policy_step, dict) else ""
            report.check(
                bool(body),
                f"{label} policy step has no shell body to inspect",
            )
            mkdir_at = body.find('mkdir -p "$dir/$(dirname "$f")"')
            redirect_at = body.find('> "$dir/$f.tmp"')
            report.check(
                mkdir_at != -1 and redirect_at != -1 and mkdir_at < redirect_at,
                f"{label} policy step must create $dir/<dirname(f)> BEFORE the "
                f'> "$dir/$f.tmp" redirect; otherwise every nested contract path '
                "(including the default .github/triage-policy.md) fails closed",
            )

    # ---- masters carry no consumer contract of their own ---------------------------------
    for consumer in CONSUMER_CONTRACT_FILES:
        for candidate in (root / consumer, root / ".github" / consumer):
            report.check(
                not candidate.exists(),
                f"{label} this repository must not ship the consumer contract file "
                f"{candidate.relative_to(root)}; the contract belongs to the caller",
            )

    # ---- generated lock: shape ----------------------------------------------------------
    report.check(
        len(re.findall(r"(?m)^  workflow_call:\s*$", lock_text)) == 1,
        f"{label} lock must declare exactly one on.workflow_call",
    )
    report.check(
        len(re.findall(r"(?m)^permissions: \{\}$", lock_text)) == 1,
        f"{label} lock must declare an empty top-level permissions and delegate to jobs",
    )
    agent_block = lock_job_block(lock_text, "agent")
    if report.check(agent_block is not None, f"{label} lock has no 'agent' job"):
        report.check(
            "actions/checkout" not in agent_block,
            f"{label} lock 'agent' job must not check out the repository",
        )

    # ---- generated lock: agent sandbox --------------------------------------------------
    report.check(
        lock_text.count("--deny-tool shell") == 1,
        f"{label} lock must pass '--deny-tool shell' exactly once, found "
        f"{lock_text.count('--deny-tool shell')}",
    )
    report.check(
        lock_text.count("--deny-tool workflow") == 1,
        f"{label} lock must pass '--deny-tool workflow' exactly once, found "
        f"{lock_text.count('--deny-tool workflow')}",
    )
    report.check(
        lock_text.count("--disable-builtin-mcps") == 1,
        f"{label} lock must disable built-in MCPs exactly once, found "
        f"{lock_text.count('--disable-builtin-mcps')}",
    )
    report.check(
        lock_text.count('"dynamic_tools": []') == 1,
        f"{label} lock must not expose dynamic safe-output tools",
    )
    report.check(
        "create_labels" not in lock_text,
        f"{label} lock must not enable automatic label creation",
    )
    for flag in ("--allow-all-tools", "--allow-all-paths", "--allow-tool write", "--allow-tool 'write'"):
        report.check(flag not in lock_text, f"{label} lock must not contain {flag!r}")

    # Both grant statements — the manifest comment block and the shell-escaped runtime command
    # actually executed by the agent job — must declare the same token set, and no other.
    comment_tokens = sorted(t for form, t in allow_tool_grants(lock_text) if form == "comment")
    runtime_tokens = sorted(t for form, t in allow_tool_grants(lock_text) if form == "runtime")
    report.check(
        comment_tokens == EXPECTED_GRANT_TOKENS,
        f"{label} lock manifest comment --allow-tool tokens must be {EXPECTED_GRANT_TOKENS}, "
        f"got {comment_tokens}",
    )
    report.check(
        runtime_tokens == EXPECTED_GRANT_TOKENS,
        f"{label} lock runtime command --allow-tool tokens must be {EXPECTED_GRANT_TOKENS}, "
        f"got {runtime_tokens}",
    )
    for offender in runtime_shell_grants(lock_text):
        report.fail(
            f"{label} lock runtime command must not grant {offender}: a shell or all-tools grant "
            "defeats the metadata-only envelope"
        )

    grants = github_grant_names(lock_text)
    tool_grants = sorted(n for n in grants if not n.startswith("toolset:"))
    toolset_grants = sorted(n for n in grants if n.startswith("toolset:"))
    report.check(
        tool_grants == sorted(EXPECTED_GITHUB_TOOLS),
        f"{label} lock GitHub MCP grants must be exactly {sorted(EXPECTED_GITHUB_TOOLS)}, got {tool_grants}",
    )
    report.check(
        toolset_grants == ["toolset:issues"],
        f"{label} lock GITHUB_TOOLSETS must be exactly 'issues', found {toolset_grants}",
    )
    for forbidden in FORBIDDEN_GITHUB_GRANTS:
        report.check(
            forbidden not in grants,
            f"{label} lock must not grant forbidden GitHub tool {forbidden!r}",
        )
    report.check(
        lock_text.count('"GITHUB_READ_ONLY": "1"') == 1,
        f"{label} lock GitHub MCP server must stay read-only",
    )
    report.check(
        len(re.findall(r'"min-integrity":\s*"none"', lock_text)) >= 1
        and not re.search(r'"min-integrity":\s*"(?!none")', lock_text),
        f"{label} lock must set min-integrity 'none' only",
    )
    report.check(
        len(re.findall(r'"repos":\s*\[\s*"\$\{\{ github\.repository \}\}"\s*\]', lock_text, re.DOTALL)) == 1,
        f"{label} lock guard must scope 'repos' to exactly ['${{{{ github.repository }}}}'] as an array",
    )

    # ---- generated lock: silence ---------------------------------------------------------
    for marker in ("add_comment", "add-comment", "add_workflow_run_comment", "comment_on_issue"):
        report.check(marker not in lock_text, f"{label} lock must not contain {marker!r}")
    report.check(
        lock_text.count('comment_id: ""') == 1 and lock_text.count("comment_url:") == 0,
        f"{label} lock must not wire a status comment (status-comment: false)",
    )
    report.check(
        'GH_AW_FAILURE_REPORT_AS_ISSUE: "false"' in lock_text
        and 'GH_AW_FAILURE_REPORT_AS_ISSUE: "true"' not in lock_text,
        f"{label} lock must report failure as issue = false",
    )
    for env_key in (
        "GH_AW_MISSING_TOOL_CREATE_ISSUE",
        "GH_AW_REPORT_INCOMPLETE_CREATE_ISSUE",
    ):
        report.check(
            f'{env_key}: "false"' in lock_text
            and f'{env_key}: "true"' not in lock_text,
            f"{label} lock must disable {env_key}",
        )
    report.check(
        "report_failed_jobs" not in lock_text,
        f"{label} lock must not report failed jobs (report-failed-jobs: false)",
    )

    # ---- generated lock: safe outputs ----------------------------------------------------
    raw_config = lock_yaml_string(lock_text, "GH_AW_SAFE_OUTPUTS_CONFIG")
    if report.check(raw_config is not None, f"{label} lock has no safe-outputs config"):
        try:
            config = parse_escaped_json(raw_config, master)
        except json.JSONDecodeError as exc:
            report.fail(f"{label} lock safe-outputs config is not JSON: {exc}")
            config = None
        if config is not None:
            report.check(
                isinstance(config, dict),
                f"{label} lock safe-outputs config must be a JSON object",
            )
        if isinstance(config, dict):
            for forbidden in FORBIDDEN_SAFE_OUTPUTS:
                report.check(
                    forbidden not in config,
                    f"{label} lock safe-outputs must not expose {forbidden!r}",
                )
            expected_keys = {"add_labels", "remove_labels"} | set(BUILTIN_SAFE_OUTPUTS)
            missing = sorted(expected_keys - set(config))
            unexpected = sorted(set(config) - expected_keys)
            report.check(
                not missing,
                f"{label} lock safe-outputs are missing expected tools {missing}",
            )
            report.check(
                sorted(config) == EXPECTED_LOCK_SAFE_OUTPUTS,
                f"{label} lock safe-outputs must be exactly {EXPECTED_LOCK_SAFE_OUTPUTS}, got {sorted(config)}",
            )
            report.check(
                not unexpected,
                f"{label} lock safe-outputs expose unexpected tools {unexpected}",
            )
            for key in ("add_labels", "remove_labels"):
                report.check(key in config, f"{label} lock safe-outputs missing {key!r}")
                block = config.get(key)
                if isinstance(block, dict):
                    report.check(
                        block.get("blocked") == EXPECTED_BLOCKED,
                        f"{label} lock {key}.blocked must be exactly the required patterns, got {block.get('blocked')}",
                    )
                    report.check(
                        block.get("max") == (add_max if key == "add_labels" else remove_max),
                        f"{label} lock {key}.max must be {add_max if key == 'add_labels' else remove_max}",
                    )
            noop = config.get("noop")
            if isinstance(noop, dict):
                report.check(
                    noop.get("max") == 1 and noop.get("report-as-issue") == "false",
                    f"{label} lock noop must preserve max=1 and report-as-issue=false",
                )
            add_block = config.get("add_labels")
            if isinstance(add_block, dict):
                report.check(
                    add_block.get("max_labels") == max_labels_per_add,
                    f"{label} lock add_labels.max_labels must be {max_labels_per_add}, "
                    f"got {add_block.get('max_labels')}",
                )
                report.check(
                    add_block.get("issue_intent") is False,
                    f"{label} lock add_labels.issue_intent must be false",
                )
                report.check(
                    add_block.get("allowed") == LABELS_ADD[master],
                    f"{label} lock add_labels.allowed must be {LABELS_ADD[master]}, got {add_block.get('allowed')}",
                )
            remove_block_lock = config.get("remove_labels")
            if isinstance(remove_block_lock, dict):
                report.check(
                    remove_block_lock.get("allowed") == LABELS_REMOVE[master],
                    f"{label} lock remove_labels.allowed must be {LABELS_REMOVE[master]}, got {remove_block_lock.get('allowed')}",
                )

    manifest = None
    match = re.search(r"(?m)^# gh-aw-manifest: (\{.*\})$", lock_text)
    if report.check(match is not None, f"{label} lock has no gh-aw-manifest line"):
        try:
            manifest = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            report.fail(f"{label} lock gh-aw-manifest is not JSON: {exc}")
    if isinstance(manifest, dict):
        gateway = next(
            (c for c in manifest.get("containers", []) if isinstance(c, dict)
             and str(c.get("image", "")).startswith("ghcr.io/github/gh-aw-mcpg:")),
            None,
        )
        report.check(
            gateway is not None
            and gateway.get("image") == MCP_GATEWAY_IMAGE
            and gateway.get("digest") == MCP_GATEWAY_DIGEST
            and gateway.get("pinned_image") == f"{MCP_GATEWAY_IMAGE}@{MCP_GATEWAY_DIGEST}",
            f"{label} lock must pin gateway {MCP_GATEWAY_IMAGE}@{MCP_GATEWAY_DIGEST}",
        )
        servers = {server.get("name"): server.get("tools") for server in manifest.get("mcp_servers", [])}
        report.check(
            servers.get("github") == EXPECTED_GITHUB_TOOLS,
            f"{label} lock manifest github tools must be {EXPECTED_GITHUB_TOOLS}, got {servers.get('github')}",
        )
        report.check(
            servers.get("safeoutputs") == EXPECTED_MANIFEST_SAFE_OUTPUTS,
            f"{label} lock manifest safeoutputs tools must be {EXPECTED_MANIFEST_SAFE_OUTPUTS}, got {servers.get('safeoutputs')}",
        )
    report.check(
        f'"compiler_version":"{GH_AW_VERSION}"' in lock_text
        and f"gh-aw ({GH_AW_VERSION})" in lock_text,
        f"{label} lock must be generated by gh-aw {GH_AW_VERSION}",
    )
    for writer in (
        "create_report_incomplete_issue",
        "create_missing_tool_issue",
        "create_missing_data_issue",
    ):
        report.check(
            f'"{writer}"' not in lock_text,
            f"{label} lock must not configure diagnostic issue writer {writer!r}",
        )

    # ---- generated lock: imports must be inlined, not deferred --------------------------
    # These masters are published for cross-repository `uses:`. At call time the caller's
    # workspace holds the CALLER's checkout, not this repository's, so a
    # `{{#runtime-import ...}}` macro naming .github/aw/imports/** could never resolve and the
    # agent prompt would lose the shared contract core. `inlined-imports: true` must therefore
    # have actually inlined the imports into the lock at compile time.
    report.check(
        "runtime-import" not in lock_text,
        f"{label} lock must not contain runtime-import macros; inlined-imports requires the "
        "shared prompt cores inlined at compile time, because a cross-repository caller's "
        "workspace cannot supply this repository's .github/aw/imports/** at run time",
    )

    # ---- pinned import cache -------------------------------------------------------------
    for entry in spec["imports"]:
        location, _, pin = entry.rpartition("@")
        owner, _, rest = location.partition("/")
        repo, _, rel_path = rest.partition("/")
        # gh-aw flattens the imported path into '<dir>_<name>' with '/' -> '_'.
        vendored = root / VENDOR_ROOT / owner / repo / pin / rel_path.replace("/", "_")
        report.check(
            vendored.is_file(),
            f"{label} pinned import {entry} is not vendored at {vendored.relative_to(root)}",
        )


def validate_shared(report: Report, root: Path) -> None:
    """Repository-wide assertions that no single master can own."""
    ci_workflow = root / ".github" / "workflows" / "validate-masters.yml"
    ci_text = ci_workflow.read_text(encoding="utf-8") if ci_workflow.is_file() else ""
    report.check(
        ci_text.count(f"GH_AW_VERSION: {GH_AW_VERSION}") == 1,
        f"validate-masters workflow must pin GH_AW_VERSION to {GH_AW_VERSION}",
    )
    import_dirs = sorted(
        p.name for p in (root / VENDOR_ROOT / SHARED_REPO).iterdir() if p.is_dir()
    ) if (root / VENDOR_ROOT / SHARED_REPO).is_dir() else []
    report.check(
        import_dirs == [SHARED_PIN],
        f"vendored import cache must contain exactly the audited pin {SHARED_PIN}, got {import_dirs}",
    )
    expected_files = {
        "contract-invariant.md",
        "issue-triage-core.md",
        "pr-intake-core.md",
        "backlog-retriage-core.md",
    }
    vendored_dir = root / VENDOR_ROOT / SHARED_REPO / SHARED_PIN
    if vendored_dir.is_dir():
        present = {
            p.name.replace("packages_ghaw-triage_workflows_", "")
            for p in vendored_dir.iterdir()
            if p.is_file()
        }
        report.check(
            present == expected_files,
            f"vendored pin {SHARED_PIN[:8]} must hold exactly {sorted(expected_files)}, got {sorted(present)}",
        )

    used = {entry for spec in MASTERS.values() for entry in spec["imports"]}
    report.check(
        len(used) == 4,
        f"the three masters must share exactly 4 pinned components, got {len(used)}",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        default=None,
        help="repository root to validate (default: the repository containing this script)",
    )
    parser.add_argument(
        "--master",
        action="append",
        choices=MASTER_IDS,
        help="validate only the named master (repeatable); default is all",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    report = Report()

    if not (root / ".github" / "workflows").is_dir():
        print(f"validate-masters: {root} does not look like a repository root", file=sys.stderr)
        return 2

    masters = args.master or MASTER_IDS
    try:
        for master in masters:
            validate_master(root, master, report)
        if len(masters) == len(MASTER_IDS):
            validate_shared(report, root)
    except ParseError as exc:
        print(f"validate-masters: FATAL: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"validate-masters: FATAL: {exc}", file=sys.stderr)
        return 2

    if report.errors:
        print(f"validate-masters: FAILED ({len(report.errors)} problem(s), {report.passed} check(s) passed)")
        for error in report.errors:
            print(f"  - {error}")
        return 1

    print(f"validate-masters: OK ({report.passed} checks passed across {len(masters)} master(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
