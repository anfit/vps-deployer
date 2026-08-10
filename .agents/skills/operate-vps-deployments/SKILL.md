---
name: operate-vps-deployments
description: Operate services already managed by vps-deployer and service-infra. Use for host inspection or onboarding, deployment validation, plan, apply, status, logs, rollback, removal, release-manifest diagnosis, and verification of an existing deployment. Do not use for making a new application deployable; use onboard-vps-service instead.
---

# Operate VPS Deployments

Operate the current SSH-based deployment estate without exposing secrets or disturbing unrelated services on shared hosts.

## Load the authoritative context

1. Read `$VPS_DEPLOYER_DIR/docs/operations.md`.
2. Read `$VPS_DEPLOYER_DIR/docs/manifest-reference.md` when interpreting a deployment.
3. Read `$SERVICE_INFRA_DIR/docs/operator-runbook.md` and the relevant entry in `$SERVICE_INFRA_DIR/docs/services.md`.
4. Read `$SERVICE_INFRA_DIR/docs/secrets.md` only when the operation needs environment-backed secrets.
5. Use [references/repository-map.md](references/repository-map.md) to resolve repositories and ownership boundaries.

Resolve repositories through `PROJECTS_DIR`, `VPS_DEPLOYER_DIR`, and `SERVICE_INFRA_DIR`. Never hardcode a workstation path and never use parent-directory traversal as repository discovery.

## Establish state safely

- Inspect Git status in every repository in scope. Preserve unrelated changes.
- Confirm the desired application branch and commit before creating a release.
- Run repository validation, then inspect the target host and deployment.
- Treat `service-infra` manifests as desired state and the application repository as release content.
- Load only secret names declared by the deployment from the Windows User environment into the current process. Never print, persist, diff, or commit secret values.

## Plan, apply, and verify

1. Run `vps-deployer --repo $env:SERVICE_INFRA_DIR validate`.
2. Run `plan DEPLOYMENT` and review identity, source, routes, storage, and privileged operations.
3. Run `apply DEPLOYMENT`. Add `--allow-privileged` only for a manifest explicitly declaring a privileged service.
4. Run `status DEPLOYMENT`.
5. Verify the loopback health check and any documented public URL.
6. Confirm the active `build.properties` reports the desired release and exact Git commit.
7. Run `plan DEPLOYMENT` again; a converged deployment must report `No changes.`

Report the deployment name, release ID, Git commit, service and health state, public check result, and final no-op result.

## Diagnose failures

- Start with `status`, then bounded `logs`; avoid following logs indefinitely unless asked to monitor.
- If apply reports that the previous release was restored, verify which release is active before doing anything else.
- Separate application health, loopback connectivity, nginx routing, DNS, and TLS checks.
- A stale or missing manifest requires a normal redeploy; never edit an immutable release in place.
- Use rollback only to activate an existing retained release. Note that desired state remains unchanged.

## Remove or repair

- Treat removal as destructive even though releases and storage are retained. Confirm the exact deployment requested.
- Inspect shared-host names, ports, routes, and storage before host-level changes.
- Do not remove service users, application data, certificates, or legacy material unless the user explicitly includes them.
- Commit or push repository changes only when requested or clearly part of the requested delivery.
