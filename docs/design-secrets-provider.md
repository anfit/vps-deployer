# Design note: secrets providers

Status: proposed. No behavior change to existing manifests.

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
