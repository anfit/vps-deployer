# Onboarding checklist

## Discovery

- Application repository and intended branch/commit
- Existing host, user, unit, port, storage, proxy, TLS, and health behavior
- Environment matrix and public URLs
- Existing secrets and non-secret configuration sources
- Legacy deployment and integration workflows
- Backup, rollback, and data-retention constraints

## Application readiness

- `.deployer/run.sh` (or a named entrypoint) as the release-local startup command
- Runtime writes redirected to persistent storage
- LF shell scripts and executable entry point
- Stable loopback health endpoint
- Local-only content excluded with `.deployer/ignore.txt`
- Managed-startup tests pass
- `vps-deployer doctor` reports no errors and every warning is understood

## Desired state

- Unique host and deployment identities
- `${PROJECTS_DIR}` source and `${SERVICE_INFRA_DIR}` includes
- Non-secrets committed; secrets declared only by environment-variable name
- Unique port, storage, health URL, proxy name, and domain per environment
- Existing certificates referenced, not issued by the deployment
- Service and secret-variable documentation updated (`.env.example`, regenerated `docs/services.md`)
- Generic changes mirrored in `service-infra-template`; parity check clean

## Cutover acceptance

- `validate` and `secrets check` succeed
- Plan contains only intended changes
- Apply succeeds without exposing secrets
- `status` matches desired release and Git commit
- Loopback and public health checks pass
- Integration checks pass where applicable
- Repeated plan reports `No changes.`
- Legacy automation is disabled and legacy cleanup matches explicit scope
