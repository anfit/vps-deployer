# Manifest reference

## Infrastructure repository

```text
hosts/*.yaml
deployments/**/*.yaml
globals/*.yaml              # optional shared non-secret values
configs/**                  # optional committed configuration included in releases
```

Deployment files are discovered recursively. Names must be unique across the
repository.

## Host

```yaml
name: prod
ssh:
  host: example.org
  user: ubuntu
  privileged_host: example.org          # optional
  privileged_user: root                 # optional
  privileged_identity_file: ~/.ssh/key  # optional
managed_root: /srv/vps-deployer         # optional default
```

`host` may be a DNS name, IP address, or OpenSSH config alias. Privileged fields
must describe the controlled elevation path when ordinary sudo is unavailable.

## Deployment

```yaml
name: example-prod
host: prod

service:
  user: svc-example
  privileged: false

release:
  source: ${PROJECTS_DIR}/example
  include:
    - source: ${SERVICE_INFRA_DIR}/configs/example/prod.yaml
      target: config/production.yaml

runtime:
  # command defaults to ./.deployer/run
  working_directory: .
  restart: always

environment:
  TZ: Europe/Warsaw
  SHARED_VALUE:
    from_global: SHARED_VALUE

secrets:
  API_TOKEN:
    from_env: EXAMPLE_API_TOKEN

storage:
  state:
    path: /var/lib/example

healthcheck:
  type: http
  url: http://127.0.0.1:5100/health

http_proxy:
  name: example-prod
  domain: example.org
  upstream: http://127.0.0.1:5100
  certificate: /etc/letsencrypt/live/example.org/fullchain.pem
  certificate_key: /etc/letsencrypt/live/example.org/privkey.pem
```

For a scheduled oneshot deployment, omit `healthcheck` and `http_proxy` and add:

```yaml
timer:
  on_calendar: "*-*-* 03:00:00"
  persistent: true
  randomized_delay_sec: 300
```

Timer deployments generate both `.service` and `.timer` units. The service is
`Type=oneshot`; the timer is enabled and supervised during apply, status,
rollback, and removal. `on_calendar` uses systemd calendar syntax. Randomized
delay is bounded to 0–86400 seconds.

For a trusted-network HTTP-only site, disable TLS explicitly and omit the
certificate fields:

```yaml
http_proxy:
  name: example-dev
  domain: dev.example.test
  upstream: http://127.0.0.1:5100
  tls: false
```

TLS defaults to `true` for backward compatibility. When enabled, both
certificate paths are required; when disabled, certificate fields are rejected.

### Service

`service.user` is the isolated Linux account. Non-root users are created when
missing. Root services require `privileged: true` and explicit
`--allow-privileged` on apply and removal.

### Release

`source` is a directory or tar.gz artifact. `include` overlays committed files
at safe relative targets. Local paths accept explicit `${NAME}` environment
roots. Missing roots, unresolved variables, and `..` traversal are rejected.

Tarball sources are copied to a private local snapshot and inspected before upload.
Only regular files and directories with release-relative paths are accepted;
links, devices, traversal, and absolute member paths are rejected.

Directory sources can provide `.deployer/ignore`. Each non-comment line is a
glob matched against repository-relative POSIX paths. Default exclusions include
Git metadata, virtual environments, IDE state, caches, common secret extensions,
and local test directories. The legacy root `.vps-deployer-ignore` is read only
when `.deployer/ignore` is absent.

### Runtime

`command` defaults to `./.deployer/run`. An explicit command supports named
application entrypoints such as `./.deployer/bootstrap`. It must begin with `./`
and resolve inside the release. The executable is
made runnable during installation. `working_directory` is release-relative.
`restart` accepts `always`, `on-failure`, or `no`.

Applications should keep runtime dependencies rollback-safe. Prefer dependencies
inside a prepared artifact, a content-addressed environment, or an environment
associated with the release. If persistent storage holds a runtime environment,
key it by dependency content rather than mutating one environment shared by all
releases.

### Environment and secrets

Plain values and `from_global` are non-secret and may be committed. Secret values
must use `secrets.<KEY>.from_env`; only the variable name is committed. Apply
requires every referenced secret to exist in the process environment. Plans
compare non-secret values and secret-key presence without reading or printing
secret values.

### Storage

Every storage path must be an absolute safe path. It is created with ownership
for the service user and added to the systemd unit's writable paths. Storage is
never deleted by rollback or remove.

### Health check

HTTP health checks use curl from the target host with bounded retries. Prefer a
loopback URL so application health is tested independently of DNS and TLS. URLs
must use `http://127.0.0.1:<port>/...` with a valid TCP port.
Health checks and HTTP proxies are not valid for timer deployments; status checks
that the timer unit is active instead.

### HTTP proxy

The proxy manages one nginx site with HTTP and HTTPS listeners. Upstreams must
use `http://127.0.0.1:<port>`. Certificates must already exist on the host.
Certificate issuance and renewal remain host-level responsibilities.
