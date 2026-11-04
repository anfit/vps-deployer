#!/bin/sh
# Application-owned entrypoint. Runs in the foreground; systemd supervises it.
exec python3 "$(dirname "$0")/serve.py"
