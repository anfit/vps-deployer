---
name: onboard-vps-service
description: Make a new or legacy application deployable through vps-deployer and service-infra. Use when adding a service, migrating legacy infrastructure or deployment workflows, creating multiple environment deployments on one VPS, adapting runtime and health behavior, or extending vps-deployer generically for a missing capability. Do not use for a routine redeploy of an existing managed service.
---

# Onboard a VPS Service

Prepare an application, its desired-state manifests, and its cutover so future operations use the standard SSH-based deployment workflow.

## Load the authoritative context

1. Read `$VPS_DEPLOYER_DIR/docs/architecture.md` and `$VPS_DEPLOYER_DIR/docs/manifest-reference.md`.
2. Read `$VPS_DEPLOYER_DIR/docs/operations.md` before planning a cutover.
3. Read `$SERVICE_INFRA_DIR/docs/services.md`, `$SERVICE_INFRA_DIR/docs/secrets.md`, and `$SERVICE_INFRA_DIR/docs/operator-runbook.md`.
4. Use [references/onboarding-checklist.md](references/onboarding-checklist.md) throughout the migration.

Resolve repositories through `PROJECTS_DIR`, `VPS_DEPLOYER_DIR`, and `SERVICE_INFRA_DIR`. Do not introduce hardcoded workstation paths or `..` traversal.

## Inventory before editing

- Read repository instructions and inspect Git status in the application, `service-infra`, and `vps-deployer`.
- Inspect the current live service read-only: user, unit, working tree or artifact, environment sources, storage, port, proxy, certificates, health endpoint, and deployment automation.
- Identify every environment independently. Multiple deployments on one host must not share deployment names, ports, writable paths, routes, or secret namespaces accidentally.
- Record rollback and data-retention constraints before cutover.

## Adapt the application

- Provide a release-local executable entry point such as `run-managed.sh`.
- Keep virtual environments, caches, state, databases, and other writes in declared persistent storage, not immutable releases.
- Read runtime configuration from committed files or environment variables. Move only actual secret values to environment-backed inputs.
- Provide a stable loopback health endpoint that tests application readiness without DNS or TLS.
- Add `.vps-deployer-ignore` for local credentials, helper files, build output, or other content that must not enter release hashes.
- Enforce LF for deployed shell scripts, commonly with `.gitattributes`.
- Add or update application tests for the managed startup path.

Reorganize the application when that produces a clean deployment boundary, but preserve unrelated behavior and user changes.

## Define desired state

- Add one recursively discoverable deployment manifest per environment in `service-infra`.
- Use `${PROJECTS_DIR}/repository-name` for application sources and `${SERVICE_INFRA_DIR}/...` for committed includes.
- Commit ordinary environment-specific configuration directly or include it from `service-infra`.
- Declare genuine secrets under `secrets` with `from_env`; never store their values in any repository.
- Give each deployment a unique service identity, release name, port, storage, health URL, and proxy route.
- Document the service, required User environment variable names, public URLs, and operational exceptions.

If the manifest model cannot express a legitimate reusable requirement, extend `vps-deployer` generically with tests and documentation. Do not add service-name conditionals.

## Migrate and cut over

1. Validate all repositories and run focused tests.
2. Arrange required Windows User secret variables without displaying their values. Request approval before copying secret material from a remote system.
3. Inspect or onboard the host as needed.
4. Review the deployment plan, then apply.
5. Verify status, generated `build.properties`, loopback health, public routing, and a repeated no-op plan.
6. Exercise application integration tests when the service contract requires them.
7. Disable legacy deploy automation once the replacement path is verified and the user has included that migration in scope.
8. Remove legacy units, files, users, or CI material only when explicitly authorized; preserve data and rollback material otherwise.

Finish by documenting the result and reporting the active release and commit for every environment. Commit and push only as requested.
