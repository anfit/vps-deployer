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
  command: ./run-managed.sh
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

Directory sources can provide `.vps-deployer-ignore`. Each non-comment line is a
glob matched against repository-relative POSIX paths. Default exclusions include
Git metadata, virtual environments, IDE state, caches, common secret extensions,
and local test directories.

### Runtime

`command` must begin with `./` and resolve inside the release. The executable is
made runnable during installation. `working_directory` is release-relative.
`restart` accepts `always`, `on-failure`, or `no`.

Applications should create or update virtual environments in declared storage,
not inside the immutable release.

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
loopback URL so application health is tested independently of DNS and TLS.

### HTTP proxy

The proxy manages one nginx site with HTTP and HTTPS listeners. Upstreams must
use `http://127.0.0.1:<port>`. Certificates must already exist on the host.
Certificate issuance and renewal remain host-level responsibilities.
