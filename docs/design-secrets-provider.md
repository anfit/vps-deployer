# Design note: secrets providers

Status: implemented in 1.4.0 (`vps_deployer/secret_sources.py`; see docs/operations.md,
"Secret sources"). The shipped design differs from the proposal below in two ways: sources
are chosen with `VPS_DEPLOYER_SECRET_SOURCES` instead of a repository-level file (secrets
are per operator, not per repository), and the default chain is `env,file`, where the file
is used only if it exists, so setups without it behave exactly as before.

No behavior change to existing manifests.

## Problem
Secrets resolve only from process environment variables (`secrets.<KEY>.from_env`).
An estate with many services needs many persistent User variables, which is hard
to audit, rotate, and move between machines.

## Proposal
Keep `from_env` unchanged. Add an optional repository-level provider chain in
`vps-deployer.yaml` (absent means env only):

```yaml
secrets:
  providers: [env, file]          # first provider that has the variable wins
  file: ~/.config/vps-deployer/secrets.env   # KEY=VALUE, mode 0600 / user-only ACL
```

- The manifest still names a variable (`from_env: SERVICE_PROD_TOKEN`); providers
  only change where the value is looked up. Manifests stay portable.
- `env` is always first, so existing behavior and CI use are unaffected.
- `file` refuses to load if permissions are broader than the current user.
- A later `keychain` provider (Windows Credential Manager, macOS Keychain,
  libsecret) uses the same lookup interface.

## Requirements
- `secrets check` and the missing-secret errors name variables only, never values.
- Values are never printed in plans, logs, or error messages (as today).
- One resolver function (`Repository.resolve_secret(name)`) is the only lookup point.

## Rollout
1. Introduce the resolver and route `missing_secrets` and `resolve_environment` through it (env only; no behavior change).
2. Add the `file` provider behind `secrets.providers`.
3. Mirror docs and `.env.example` handling in the template and instance.
