---
name: Backlog Re-triage
description: Reusable Backlog Re-triage reconciling triage metadata for a bounded batch of issues/PRs in the calling repository (metadata-only).
on:
  workflow_call:
    inputs:
      contract_files:
        description: >-
          Space-separated list of caller-repository contract files resolved at the
          trusted Policy SHA and exposed read-only under .policy/<POLICY_SHA>/.
        required: false
        type: string
        default: ".github/triage-policy.md AGENTS.md CONTRIBUTING.md .github/ISSUE_TEMPLATE/bug_report.yml .github/PULL_REQUEST_TEMPLATE.md .github/labels.yml"
      item_numbers:
        description: >-
          Optional comma-separated issue/PR numbers to reconcile, at most 10. When
          empty the pre-agent step selects the 10 least-recently-updated open items.
        required: false
        type: string
  status-comment: false
permissions:
  contents: read
  issues: read
  pull-requests: read
engine:
  id: copilot
  model: glm-5.3-flash
  bare: true
  args: ["--deny-tool", "shell"]
  concurrency:
    group: "gh-aw-triage-${{ github.repository }}"
    queue: max
  env:
    COPILOT_PROVIDER_BASE_URL: "https://ollama.com/v1"
    COPILOT_PROVIDER_API_KEY: ${{ secrets.OLLAMA_API_KEY }}
    COPILOT_PROVIDER_TYPE: openai
models:
  default-ai-credits-pricing:
    input: 0.000001
    output: 0.000001
inlined-imports: true
imports:
  - bashrusakh/repo-docs-sync/packages/ghaw-triage/workflows/contract-invariant.md@e2de4a989077b7fdf558ef5656040dc2e547e674
  - bashrusakh/repo-docs-sync/packages/ghaw-triage/workflows/backlog-retriage-core.md@e2de4a989077b7fdf558ef5656040dc2e547e674
checkout: false
max-ai-credits: 10
max-turns: 20
timeout-minutes: 20
concurrency:
  job-discriminator: ${{ github.run_id }}
network:
  allowed: [defaults, github, ollama.com]
tools:
  bash: false
  cli-proxy: false
  edit: false
  github:
    mode: local
    toolsets: [issues]
    # Declared scope only, not a runtime guarantee (a gateway safety net may widen it):
    # 'repos' is the calling repository — exactly the expression below. In a reusable
    # workflow the github context (and therefore ${{ github.repository }}) is always the
    # CALLER's, so label safe outputs and contract reads target the calling repository.
    # min-integrity: none is deliberate: triage must read reports from any contributor, and
    # gh-aw docs prescribe 'none' for public-repo triage. Otherwise metadata-only (no
    # shell/source/diff; safe outputs are label adds/removes only), so the injection surface
    # is metadata-only. If abuse appears, add blocked-users / trusted-users / approval-labels.
    allowed-repos: ["${{ github.repository }}"]
    min-integrity: none
    # max-calls is declared intent; gh-aw v0.91.4 currently drops it at compile time (no tool-call-limits in locks). Revisit when the compiler emits limits.
    # No 'pull_requests' toolset: it exposes pull_request_read (with get_diff/get_files)
    # and list_pull_requests, which the CLI does not filter down to the declared allowed
    # list, so a PR-search capability would become agent-visible. Item metadata, including
    # changed filenames where a batch item is a PR, comes from the prepared
    # .policy/backlog/<POLICY_SHA>/batch.json; related/duplicate reasoning over PRs uses
    # that supplied metadata only.
    allowed:
      - name: issue_read
        max-calls: 12
      - name: search_issues
        max-calls: 3
pre-agent-steps:
  - name: Resolve repository policy contract at the trusted Policy SHA
    env:
      GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      # In a reusable workflow this is the CALLING repository, which owns the contract.
      GITHUB_REPO: ${{ github.repository }}
      # Backlog re-triage is a caller-scheduled sweep, so the trusted Policy SHA is the
      # caller's current default-branch head. A batch item that is a PR never contributes
      # its head branch as a policy source.
      POLICY_REF: ${{ github.event.repository.default_branch || 'main' }}
      CONTRACT_FILES: ${{ inputs.contract_files }}
    run: |
      set -euo pipefail
      if [ -z "${CONTRACT_FILES// /}" ]; then
        echo "::error::contract_files is empty; at least one caller contract file is required."
        exit 1
      fi
      sha=$(gh api "repos/$GITHUB_REPO/commits/$POLICY_REF" --jq .sha)
      dir=".policy/$sha"; mkdir -p "$dir"
      for f in $CONTRACT_FILES; do
        # Create the parent directory first: the redirect target is
        # "$dir/$f.tmp", so a nested contract path needs the directory to exist
        # before the shell opens the file.
        mkdir -p "$dir/$(dirname "$f")"
        if ! gh api "repos/$GITHUB_REPO/contents/$f?ref=$sha" --jq .content 2>/dev/null | base64 -d > "$dir/$f.tmp"; then
          rm -f "$dir/$f.tmp"
          echo "::error::Contract file '$f' is missing in $GITHUB_REPO at $sha (contract_files='$CONTRACT_FILES'). Fix the caller's contract_files input; refusing to run triage with an incomplete contract."
          exit 1
        fi
        mv "$dir/$f.tmp" "$dir/$f"
      done
      echo "POLICY_SHA=$sha" >> "$GITHUB_ENV"
      echo "Resolved policy contract at $sha from contract_files: $CONTRACT_FILES"
  - name: Resolve bounded backlog batch
    env:
      GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      GITHUB_REPO: ${{ github.repository }}
      ITEM_NUMBERS: ${{ inputs.item_numbers }}
    run: |
      set -euo pipefail
      if [ -z "${POLICY_SHA:-}" ]; then
        echo "::error::No trusted Policy SHA is available; the contract was not resolved. Refusing to build a batch."
        exit 1
      fi
      batch_dir=".policy/backlog/${POLICY_SHA}"
      if [ -e "${batch_dir}" ]; then chmod -R u+w "${batch_dir}" 2>/dev/null || true; rm -rf "${batch_dir}"; fi
      mkdir -p "${batch_dir}"
      numbers="$(printf '%s' "${ITEM_NUMBERS:-}" | tr ',' '\n' | tr -dc '0-9\n' | grep -E '^[0-9]+$' | head -n 10 || true)"
      if [ -n "${numbers}" ]; then
        tmp="$(mktemp)"
        resolved=0
        while IFS= read -r n; do
          [ -n "${n}" ] || continue
          if item="$(gh api "repos/${GITHUB_REPO}/issues/${n}" \
            --jq '{number, title, state, labels: [.labels[].name]}' 2>/dev/null)"; then
            [ -n "${item}" ] || continue
            printf '%s\n' "${item}" >> "${tmp}"
            resolved=$((resolved + 1))
          fi
        done <<< "${numbers}"
        # Explicitly requested items are a request for exactly those items: if none of
        # them is resolvable there is nothing to reconcile, and failing closed beats
        # silently re-triageing a different set. An empty batch is only legitimate when
        # the repository itself has nothing to offer (the auto-selection path below).
        if [ "${resolved}" -eq 0 ]; then
          rm -f "${tmp}"
          {
            echo "### Backlog batch not prepared"
            echo ""
            echo "None of the \`item_numbers\` request was resolvable in ${GITHUB_REPO}, so no bounded batch could be established."
            echo "Nothing was read or written. Re-run with existing issue/PR numbers, or with \`item_numbers\` empty to select the least-recently-updated open items."
          } >> "$GITHUB_STEP_SUMMARY"
          echo "None of the requested item_numbers resolved in ${GITHUB_REPO}; failing closed" >&2
          exit 1
        fi
        jq -s '[.[] | select(.number != null)]' "${tmp}" > "${batch_dir}/batch.json"
        rm -f "${tmp}"
      else
        # The issues endpoint returns pull requests too; both are legitimate backlog items
        # for managed-label reconciliation. Ascending 'updated' selects the stalest items.
        if ! gh api "repos/${GITHUB_REPO}/issues?state=open&sort=updated&direction=asc&per_page=10" \
          --jq 'map({number, title, state, labels: [.labels[].name]})' > "${batch_dir}/batch.json"; then
          printf '[]\n' > "${batch_dir}/batch.json"
          {
            echo "### Backlog batch not prepared"
            echo ""
            echo "The open-item query for ${GITHUB_REPO} failed, so no bounded batch could be established."
            echo "Nothing was read or written."
          } >> "$GITHUB_STEP_SUMMARY"
          echo "Open-item query failed in ${GITHUB_REPO}; failing closed" >&2
          exit 1
        fi
        # An empty repository backlog is a legitimate no-op: the batch is valid and empty.
      fi
      jq -e 'type == "array"' "${batch_dir}/batch.json" > /dev/null
      chmod 0444 "${batch_dir}/batch.json"
      chmod 0555 "${batch_dir}"
      printf 'Bounded backlog batch (%s items) at %s\n' "$(jq 'length' "${batch_dir}/batch.json")" "${batch_dir}/batch.json"
safe-outputs:
  report-failure-as-issue: false
  report-failed-jobs: false
  missing-tool:
    create-issue: false
  missing-data:
    create-issue: false
  report-incomplete:
    create-issue: false
  noop:
    max: 1
    report-as-issue: false
  add-labels:
    # Disable issue-intent metadata (rationale/confidence/suggest) for label adds: the
    # exposed tool schema drops those fields and the handler never routes a label through
    # pending-suggestion review. Triage applies labels directly; a suggestion would be a
    # silent no-op. (remove-labels has no suggestion path and rejects this key.)
    issue-intent: false
    allowed: ["bug", "enhancement", "documentation", "question", "refactor", "ci", "needs-info", "duplicate"]
    blocked: ["priority-*", "codex-*", "confirmed", "invalid", "wontfix", "good first issue", "help wanted", "~*", "*[bot]"]
    max: 5
  remove-labels:
    allowed: ["bug", "enhancement", "documentation", "question", "refactor", "ci", "needs-info", "duplicate"]
    blocked: ["priority-*", "codex-*", "confirmed", "invalid", "wontfix", "good first issue", "help wanted", "~*", "*[bot]"]
    max: 5
---

# Workflow 4 — Backlog Re-triage (central master)

Reconcile triage metadata for the bounded set of existing issues/PRs supplied to this
workflow. This is the reusable (workflow_call) deployment of **Workflow 4 — Backlog
Re-triage**; the calling repository provides only a trigger shim, and the caller owns the
schedule (a reusable workflow carries no schedule of its own). Follow the imported
`backlog-retriage-core.md` prompt core exactly for the mission, authority rules, and the
mutation surface; this body only adds the mandatory invariant, the trusted Policy-SHA
mechanics, the bounded-batch rule, and the managed label boundary.

## Caller-repository contract

This workflow is reusable and carries no contract of its own. In a reusable workflow the
`github` context — including `github.repository` — is always the **calling** repository, so
every repository read here is a read of the caller's repository. The repository contract for
this run therefore comes from the caller: the pre-agent step has resolved the caller's
default-branch head (the trusted **Policy SHA**) and fetched the caller's contract files
read-only under `.policy/<POLICY_SHA>/`.

The contract file list is the caller's `contract_files` input (default:
`.github/triage-policy.md AGENTS.md CONTRIBUTING.md .github/ISSUE_TEMPLATE/bug_report.yml
.github/PULL_REQUEST_TEMPLATE.md .github/labels.yml`). A caller must declare a list whose
files all exist in its own repository: the pre-agent step fails closed, before the agent
starts, if any listed file is missing. Trim the list to what the caller actually has.

## Mandatory contract invariant

Resolve the current repository contract from the trusted Policy SHA before making
policy-sensitive conclusions. Treat templates as evidence/input schemas according to
authoritative repository policy, not as independent mandatory checklists. Current
repository policy outranks stale automated conclusions. Contract drift invalidates only
conclusions it can materially affect.

## Trusted Policy SHA

The pre-agent step "Resolve repository policy contract at the trusted Policy SHA" has
already resolved the trusted Policy SHA for this run — the calling repository's current
default-branch head — and fetched the caller-declared authoritative contract files
read-only under `.policy/<POLICY_SHA>/`. `POLICY_SHA` is exported to the environment of this
run.

Read the current repository contract only from `.policy/<POLICY_SHA>/`, restricted to the
files the caller declared via `contract_files`. A file the caller did not declare is not
available and is not part of this run's contract.

Current repository state and authoritative maintainer decisions outrank old automated
snapshots. The current Policy SHA outranks stale automated conclusions.

## Bounded batch (cap 10)

Process only the bounded batch the pre-agent step wrote to
`.policy/backlog/<POLICY_SHA>/batch.json` (at most 10 items). Do not expand the batch into
an all-repository sweep. If one item needs disproportionate investigation, preserve the
affected managed metadata and move on; use `noop` if a completion signal is required.

An empty batch is a legitimate no-op: there is nothing to reconcile, so make no change and
signal completion with `noop` if required. Do not compensate for an empty batch by
selecting items of your own.

Related/duplicate/fixed/supersession reasoning over pull requests is bounded to the
supplied batch metadata: this deployment exposes no PR-search or PR-list capability, so do
not attempt or claim to look up pull requests outside the supplied batch by search, list,
or identifier. Judge relatedness from the supplied titles, bodies, labels, and state only;
when that evidence cannot establish equivalence, preserve the affected state.

## Managed label boundary

- Managed across the union of triage families: `bug`, `enhancement`, `documentation`,
  `question`, `refactor`, `ci`, `needs-info`, `duplicate`. The managed set is fixed by this
  master; the caller's repository policy may further restrict what triage may change.
- Type reconciliation: preserve a correct contributor-applied type, fill a type that is
  clearly missing, and replace a clearly incorrect managed type (remove the wrong type and
  add the correct one). Make no change when the type is ambiguous — preserve the affected
  state rather than churning it.
- `confirmed` is human/verification-owned and out of scope: this workflow does not own it
  and must never add or remove it.
- Human-reserved (never add or remove; never infer): `priority-*`, `codex-*`,
  `approved-for-fix`, `codex-fixing`, `ready-for-human-review`, `invalid`, `wontfix`,
  `good first issue`, `help wanted`, and anything not listed as managed.
- Priority is maintainer-owned. Do not create labels. Do not auto-close/reopen issues,
  close/merge PRs, modify code, assign users, or post routine comments. This workflow
  grants no prerequisite/approval/decision-gate semantics to automation.

This workflow is metadata-only and silent: the agent produces no human-facing output — it
does not reproduce, validate, review code, read source/diff, or comment — and the only
repository writes it performs are the label safe outputs. When evidence is insufficient,
preserve the affected managed metadata and use `noop` if a completion signal is required —
the agent has no explanatory output channel. `needs-info` means metadata/routing
information is missing; it never means implementation proof is missing. This scopes the
silence claim to the agent and its safe outputs; operator diagnostics are separate, and
gh-aw may still file run-failure or detection diagnostics as repository-level issues
outside this workflow's agent and safe outputs.

Prefer preserving an existing state over speculative churn. Use only this workflow's safe
outputs (`add-labels`, `remove-labels`). Request label adds and removes directly as plain
label names; never attach `suggest`, `rationale`, or `confidence` intent metadata — this
deployment does not use suggestion/intent review, and a suggested label is not applied.
