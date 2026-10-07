# Reusable metadata triage

Issue, PR, and backlog callers use
`bashrusakh/.github/.github/workflows/triage-<kind>.lock.yml@main`.
This intentionally trusts the central repository's `main`: later central model,
endpoint, and workflow changes take effect without editing callers. Unlike an
immutable revision, it does not freeze the reviewed workflow implementation.
Compiler, action, container, and shared prompt-import pins remain independent.

## Credential setup

Provision an Actions secret named `TRIAGE_API_KEY` in each calling repository
(or an organization secret explicitly available to that repository). Its value
must be a valid key for the provider configured by the central master. Callers
forward only that secret, not `secrets: inherit`:

```yaml
secrets:
  TRIAGE_API_KEY: ${{ secrets.TRIAGE_API_KEY }}
```

The masters consume it as `COPILOT_PROVIDER_API_KEY` for Copilot BYOK. The current
model is `mimo-v2.6-flash-free` at `https://opencode.ai/zen/v1`. Keep provider
configuration central and the incoming secret name neutral. Switching providers
may still require rotating the key value; unchanged wiring does not provision
credentials. Never place key values in workflow files.

Callers retain their own triggers, permissions, and `contract_files`. The
metadata-only tool and label restrictions and trusted caller-policy resolution
are unchanged.
