#!/bin/bash
# Set up herdr's API socket on every shell open.
#
# The socket lives on tmpfs (/tmp) rather than the persistent home mount
# because virtiofs (macOS) rejects chmod on sockets. The path is per-user
# with a random suffix: it prevents predictable-path attacks in /tmp and
# avoids collisions between concurrent shells (each shell gets its own
# socket directory). herdr resolves its socket via HERDR_SOCKET_PATH.
_herdr_dir=$(mktemp -d "/tmp/herdr-${KLANGK_USER_ID:-default}-XXXXXXXX")
export HERDR_SOCKET_PATH="$_herdr_dir/herdr.sock"
