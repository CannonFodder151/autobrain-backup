#!/bin/sh
# AUT-2515: Bootstrap script that chowns volumes to the backup user (10001)
# before exec'ing the server as that user.
# Runs as root (Docker USER root) then drops privileges via su-exec.

set -e

chown -R 10001:10001 /config /backups

exec su-exec 10001 python3 server.py "$@"
