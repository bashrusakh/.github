#!/usr/bin/env python3
"""Negative tests for ``validate-masters.py``: every tamper below must fail closed.

Each case copies the real repository into a temporary directory, applies exactly one tamper,
runs the validator with ``--root`` against that copy, and asserts a non-zero exit plus the
error text that identifies the tamper. A pristine-copy control case runs first, so a validator
that simply failed everything could not pass this suite.

The two runtime cases are the regression guard for the escaped grant forms: gh-aw writes the
agent command as ``/bin/bash -c '... --allow-tool '\\''github(issue_read)'\\'' ...'``, and the
scanner must read the grants out of that executed command, not just the manifest comment block.

Stdlib only, no network, deterministic.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = Path(__file__).resolve().with_name("validate-masters.py")

#: gh-aw's shell-escaped quote sandwich inside the ``/bin/bash -c '...'`` command.
ESCAPED_QUOTE = "'\\''"

#: The exact runtime grant token gh-aw emits for the declared GitHub tools.
RUNTIME_ISSUE_READ = f"{ESCAPED_QUOTE}github(issue_read){ESCAPED_QUOTE}"

#: ``(case, edits, expected error substrings)``. Each edit is ``(relative path, old, new)``
#: and must match once; an optional fourth item sets the expected match count for repeated
#: generated config copies, so a tamper cannot silently apply to the wrong place.
CASES = [
    (
        "add_labels_per_call_limit_widened_in_source",
        [
            (
                ".github/workflows/triage-issue.md",
                "  add-labels:\n    max-labels: 3\n",
                "  add-labels:\n    max-labels: 10\n",
            )
        ],
        ("safe-outputs.add-labels.max-labels must be 3",),
    ),
    (
        "add_labels_per_call_limit_widened_in_lock",
        [
            (
                ".github/workflows/triage-issue.lock.yml",
                '\\"max_labels\\":3',
                '\\"max_labels\\":10',
                2,
            )
        ],
        ("lock add_labels.max_labels must be 3",),
    ),
    (
        "noop_limit_widened",
        [
            (
                ".github/workflows/triage-issue.md",
                "  noop:\n    max: 1\n    report-as-issue: false\n",
                "  noop:\n    max: 2\n    report-as-issue: false\n",
            )
        ],
        ("safe-outputs.noop.max must remain 1",),
    ),
    (
        "workflow_tool_not_denied",
        [
            (
                ".github/workflows/triage-pr.lock.yml",
                "--deny-tool workflow ",
                "",
            )
        ],
        ("must pass '--deny-tool workflow' exactly once, found 0",),
    ),
    (
        "dynamic_safeoutput_added",
        [
            (
                ".github/workflows/triage-backlog.lock.yml",
                '"dynamic_tools": []',
                '"dynamic_tools": [{"name":"unexpected"}]',
            )
        ],
        ("must not expose dynamic safe-output tools",),
    ),
    (
        "ci_compiler_pin_mismatch",
        [
            (
                ".github/workflows/validate-masters.yml",
                "  GH_AW_VERSION: v0.91.4\n",
                "  GH_AW_VERSION: v0.89.21\n",
            )
        ],
        ("validate-masters workflow must pin GH_AW_VERSION to v0.91.4",),
    ),
    (
        "compiled_gateway_pin_mismatch",
        [
            (
                ".github/workflows/triage-issue.lock.yml",
                '"image":"ghcr.io/github/gh-aw-mcpg:v0.4.29"',
                '"image":"ghcr.io/github/gh-aw-mcpg:v0.4.28"',
            )
        ],
        ("lock must pin gateway ghcr.io/github/gh-aw-mcpg:v0.4.29",),
    ),
    (
        "report_incomplete_issue_writer_enabled",
        [
            (
                ".github/workflows/triage-issue.md",
                "  report-incomplete:\n    create-issue: false\n",
                "  report-incomplete:\n    create-issue: true\n",
            )
        ],
        ("safe-outputs.report-incomplete.create-issue must be false",),
    ),
    (
        "missing_tool_issue_writer_enabled",
        [
            (
                ".github/workflows/triage-backlog.md",
                "  missing-tool:\n    create-issue: false\n",
                "  missing-tool:\n    create-issue: true\n",
            )
        ],
        ("safe-outputs.missing-tool.create-issue must be false",),
    ),
    (
        "forbidden_tool_in_source_allowed",
        [
            (
                ".github/workflows/triage-pr.md",
                "      - name: search_issues\n        max-calls: 2\n",
                "      - name: search_issues\n        max-calls: 2\n"
                "      - name: pull_request_read\n        max-calls: 1\n",
            )
        ],
        ("grants forbidden GitHub tool 'pull_request_read'",),
    ),
    (
        "deny_shell_removed_from_lock",
        [
            (
                ".github/workflows/triage-backlog.lock.yml",
                " --deny-tool shell",
                "",
            )
        ],
        ("must pass '--deny-tool shell' exactly once, found 0",),
    ),
    (
        "contract_files_names_master_side_file",
        [
            (
                ".github/workflows/triage-backlog.md",
                (
                    '        default: ".github/triage-policy.md AGENTS.md CONTRIBUTING.md'
                    ' .github/ISSUE_TEMPLATE/bug_report.yml .github/PULL_REQUEST_TEMPLATE.md'
                    ' .github/labels.yml"'
                ),
                (
                    '        default: ".github/aw/imports/bashrusakh/repo-docs-sync/'
                    "e2de4a989077b7fdf558ef5656040dc2e547e674/"
                    'packages_ghaw-triage_workflows_contract-invariant.md"'
                ),
            )
        ],
        ("resolves to a master-side file",),
    ),
    (
        "forbidden_grant_in_runtime_command",
        [
            (
                ".github/workflows/triage-pr.lock.yml",
                RUNTIME_ISSUE_READ,
                f"{ESCAPED_QUOTE}github(pull_request_read){ESCAPED_QUOTE}",
            )
        ],
        (
            "runtime command --allow-tool tokens must be",
            "must not grant forbidden GitHub tool 'pull_request_read'",
        ),
    ),
    (
        "shell_grant_in_runtime_command",
        [
            (
                ".github/workflows/triage-pr.lock.yml",
                "--no-ask-user ",
                f"--no-ask-user --allow-tool {ESCAPED_QUOTE}shell{ESCAPED_QUOTE} ",
            )
        ],
        ("must not grant --allow-tool shell",),
    ),
    (
        "policy_step_creates_parent_late",
        # The pre-#3 ordering: the redirect opens "$dir/$f.tmp" before its parent directory
        # exists. This must fail for triage-issue, whose expectation is now asserted.
        [
            (
                ".github/workflows/triage-issue.md",
                "        # Create the parent directory first: the redirect target is\n"
                '        # "$dir/$f.tmp", so a nested contract path needs the directory to exist\n'
                "        # before the shell opens the file.\n"
                '        mkdir -p "$dir/$(dirname "$f")"\n'
                "        if !",
                "        if !",
            ),
            (
                ".github/workflows/triage-issue.md",
                '          exit 1\n        fi\n        mv "$dir/$f.tmp" "$dir/$f"',
                '          exit 1\n        fi\n        mkdir -p "$dir/$(dirname "$f")"\n'
                '        mv "$dir/$f.tmp" "$dir/$f"',
            ),
        ],
        ("must create $dir/<dirname(f)> BEFORE",),
    ),
]


def run_validator(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def copytree(destination: Path) -> None:
    shutil.copytree(REPO_ROOT, destination, ignore=shutil.ignore_patterns(".git"))


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="validate-masters-neg-") as tmp:
        workspace = Path(tmp)

        control = workspace / "control"
        copytree(control)
        result = run_validator(control)
        print(f"[control:pristine_tree] exit={result.returncode}")
        if result.returncode != 0:
            failures.append(
                "control case: a pristine copy must pass the validator, got "
                f"exit {result.returncode}\n{result.stdout}{result.stderr}"
            )

        for case, edits, expected in CASES:
            root = workspace / case
            copytree(root)
            applied = True
            for edit in edits:
                rel_path, old, new = edit[:3]
                expected_occurrences = edit[3] if len(edit) == 4 else 1
                target = root / rel_path
                text = target.read_text(encoding="utf-8")
                occurrences = text.count(old)
                if occurrences != expected_occurrences:
                    failures.append(
                        f"{case}: tamper anchor occurs {occurrences} times in {rel_path}, "
                        f"expected {expected_occurrences}"
                    )
                    applied = False
                    continue
                target.write_text(
                    text.replace(old, new, expected_occurrences), encoding="utf-8"
                )
            if not applied:
                continue

            result = run_validator(root)
            output = result.stdout + result.stderr
            ok = result.returncode != 0 and all(marker in output for marker in expected)
            print(f"[{case}] exit={result.returncode} expected_error_seen={ok}")
            for line in output.splitlines():
                if "  - " in line and any(marker in line for marker in expected):
                    print(f"    {line.strip()}")
            if not ok:
                failures.append(
                    f"{case}: expected fail-closed with {expected}, got exit "
                    f"{result.returncode}\n{output}"
                )

    if failures:
        print(f"test-validate-masters: FAILED ({len(failures)} problem(s))")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"test-validate-masters: OK ({len(CASES)} tamper case(s) failed closed, control passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
