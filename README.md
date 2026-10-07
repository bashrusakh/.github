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

The masters consume it as `COPILOT_PROVIDER_API_KEY` for Copilot BYOK with provider
type `openai` (Ollama's OpenAI-compatible API). The configured model is
`glm-5.3-flash` at `https://ollama.com/v1`, with `ollama.com` as the provider
network allowance. Keep provider configuration central and the incoming secret
name neutral.

Before running provider inference, rotate existing OpenCode key values to
Ollama API keys in each caller's `TRIAGE_API_KEY` secret. Unchanged wiring does
not provision credentials; OpenCode keys and OpenAI OAuth credentials do not
authenticate to this Ollama endpoint. GitHub does not expose stored secret
values for recovering an old `OLLAMA_API_KEY`; obtain a valid Ollama key instead.
Never place key values in workflow files.

Local validation and compilation do not verify authentication, model access,
or available Ollama quota. The previous deployment encountered a weekly quota
limit; restoring its configuration does not establish that quota is available
now. No operational canary or successful provider inference is asserted here.

Callers retain their own triggers, permissions, and `contract_files`. The
metadata-only tool and label restrictions and trusted caller-policy resolution
are unchanged.
