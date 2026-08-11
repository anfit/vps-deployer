# Architecture

`vps-deployer` reconciles committed desired state from an infrastructure
repository with Linux hosts reachable through OpenSSH. Application repositories
contain code; the infrastructure repository contains deployment identity,
runtime configuration, routing, storage, and secret references.

## Ownership boundaries

- The application repository owns executable code and the `.deployer/` integration contract.
- The infrastructure repository owns hosts and deployment manifests.
- The operator environment owns secret values and local repository roots.
- The remote host owns persistent runtime data, TLS keys, and the retained rollback release.
- `vps-deployer` owns generated systemd units, environment files, release trees,
  active-release symlinks, and declared nginx sites.

One host may run any number of isolated deployments. A deployment name is the
stable identity used for its systemd unit, release directory, environment file,
and optional nginx route. Deployments on the same host must use distinct names,
loopback ports, storage paths, and proxy names.

## Remote layout

For deployment `example-prod` under the default managed root:

```text
/srv/vps-deployer/example-prod/
  releases/<release-id>/
  current -> releases/<release-id>
  previous -> releases/<release-id>
/etc/vps-deployer/example-prod.env
/etc/vps-deployer/example-prod.state
/etc/systemd/system/vps-deployer-example-prod.service
/etc/nginx/sites-available/<proxy-name>      # when http_proxy is declared
/etc/nginx/sites-enabled/<proxy-name>
```

Writable application state belongs outside releases, normally below `/var/lib`.
Releases are immutable and owned by root with read/execute access for the service
user. `remove` intentionally retains releases, storage, and service users.

## Release identity and manifest

Release IDs are source-state-addressed. The hash covers deployable source files,
release includes, and the Git commit when the source is versioned, so identical
deployable bytes from different commits intentionally produce different IDs. Ignored local
files and `.git` do not enter the archive.

Every installed release receives deployment provenance in the separate,
application-facing [`build.properties` contract](build-properties.md). Existing
application/build fields are preserved:

```properties
deployment.release=4d9901a72c81e240
deployment.commit=<full Git SHA>
deployment.commit-time=<ISO-8601 commit time>
deployment.timestamp=<UTC deployment time>
deployment.branch=<checked-out branch>
deployment.actor=vps-deployer
```

For unversioned artifacts, commit fields are omitted and the branch is
`unversioned`. `status` checks `deployment.release` and, when available,
`deployment.commit`.
A running service with a missing or stale manifest is reported unhealthy.

## Apply and rollback

An apply uploads a new release only when its release ID is absent. It writes the
environment and unit, activates the new symlink, restarts the service, and runs
the health check. If activation fails, the former environment, service and timer
units are restored along with `current` before the previous service is restarted.
An unhealthy first deployment is stopped and reported as failed. After a successful activation,
the former active release is recorded explicitly by the `previous` symlink. After the complete deployment
succeeds, older releases are deleted; only the active release and its immediate
predecessor remain. Cleanup never runs after a failed activation.

Apply holds a non-blocking host lock at
`/run/lock/vps-deployer-<deployment>.lock` across the complete multi-command
transaction. Concurrent applies for the same deployment fail without mutating
state; independent deployments remain parallelizable.

Nginx changes are serialized under a host-wide deployer lock. A candidate site is
temporarily enabled and validated before it atomically replaces the managed site;
failed validation restores the former enabled site and triggers application
rollback. Each deployment reconciles only its owned proxy file.

Scheduled deployments add a systemd `.timer` beside a sandboxed oneshot service.
The timer, rather than the short-lived service, is enabled and used for health
and lifecycle operations.

The state file records optional resources owned by the deployment. A later apply
reconciles absence as desired state: obsolete timers and nginx sites are disabled
and removed, and service/timer mode transitions disable the former supervisor
after the replacement is healthy.

Deployments created by clients predating the state file require a one-time
adoption apply with their existing optional resources still declared. This
records ownership before a subsequent manifest removes or renames those
resources; see the operations guide.

## Privilege model

Normal SSH access is used for inspection and unprivileged commands. Root-owned
files and systemd operations use either the host account's scoped sudo access or
a separate privileged SSH identity. A privileged identity should be restricted
server-side with `tools/vps-deployer-ssh-gate`; it must not provide a general
interactive root shell. The client sends operation names rather than executable
names. The gate constructs fixed commands for those capabilities and is installed
with an explicit managed root and allowlisted storage paths, so the key cannot
operate on unrelated state elsewhere under `/var/lib`. Root-owned configuration
is streamed to an atomic `write-file` capability; secret content is never staged
at an unprivileged or shared temporary pathname.
