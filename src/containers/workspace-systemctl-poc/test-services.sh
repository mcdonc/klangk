#!/bin/bash
# Test script: run inside the container to install and verify services.
# Usage: podman exec <container> bash /test-services.sh
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
RESET='\033[0m'

pass() { echo -e "${GREEN}PASS${RESET}: $1"; }
fail() {
  echo -e "${RED}FAIL${RESET}: $1"
  FAILURES=$((FAILURES + 1))
}
section() { echo -e "\n${BOLD}=== $1 ===${RESET}"; }

FAILURES=0

# --- Install packages ---
section "Installing packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  nginx \
  redis-server \
  cron \
  openssh-server \
  postgresql

# --- nginx ---
section "nginx"
systemctl start nginx
if systemctl status nginx | grep -q "running"; then
  pass "nginx status reports running"
else
  fail "nginx status does not report running"
fi
if curl -sf http://localhost:80 >/dev/null 2>&1; then
  pass "nginx responds on port 80"
else
  fail "nginx does not respond on port 80"
fi
systemctl stop nginx
if ! curl -sf http://localhost:80 >/dev/null 2>&1; then
  pass "nginx stopped (port 80 closed)"
else
  fail "nginx still responding after stop"
fi

# --- redis ---
section "redis-server"
systemctl start redis-server
if systemctl status redis-server | grep -q "running"; then
  pass "redis-server status reports running"
else
  fail "redis-server status does not report running"
fi
if command -v redis-cli >/dev/null && redis-cli ping 2>/dev/null | grep -q PONG; then
  pass "redis-cli ping returns PONG"
else
  fail "redis-cli ping did not return PONG"
fi
systemctl stop redis-server

# --- cron ---
section "cron"
systemctl start cron
if systemctl status cron | grep -q "running"; then
  pass "cron status reports running"
else
  fail "cron status does not report running"
fi
systemctl stop cron

# --- ssh ---
section "openssh-server"
mkdir -p /run/sshd
ssh-keygen -A 2>/dev/null || true
systemctl start ssh 2>/dev/null
if systemctl status ssh 2>/dev/null | grep -q "running"; then
  pass "ssh status reports running"
else
  fail "ssh status does not report running"
fi
systemctl stop ssh 2>/dev/null

# --- postgresql ---
section "postgresql"
PG_VER=$(find /etc/postgresql/ -maxdepth 1 -mindepth 1 -printf '%f\n' | head -1)
# Ensure the cluster exists
if [ ! -d "/var/lib/postgresql/${PG_VER}/main" ]; then
  su - postgres -c "pg_createcluster ${PG_VER} main" 2>/dev/null || true
fi
# systemctl3.py may not support template units (postgresql@17-main),
# so start postgres directly via pg_ctlcluster as a fallback.
systemctl start postgresql 2>/dev/null
if systemctl status postgresql 2>/dev/null | grep -q "running"; then
  pass "postgresql status reports running"
else
  fail "postgresql status does not report running"
fi
# The wrapper service may not actually start the cluster; try directly.
if ! su - postgres -c "pg_isready" >/dev/null 2>&1; then
  su - postgres -c "pg_ctlcluster ${PG_VER} main start" 2>/dev/null || true
  sleep 1
fi
if su - postgres -c "psql -c 'SELECT 1'" 2>/dev/null | grep -q "1"; then
  pass "psql SELECT 1 succeeds"
else
  fail "psql SELECT 1 failed"
fi
systemctl stop postgresql 2>/dev/null

# --- enable + restart test (nginx) ---
section "enable + restart persistence"
systemctl enable nginx
echo "To test enable persistence, restart the container and check if nginx is running."

# --- Summary ---
section "Summary"
if [ "$FAILURES" -eq 0 ]; then
  echo -e "${GREEN}All tests passed.${RESET}"
else
  echo -e "${RED}${FAILURES} test(s) failed.${RESET}"
  exit 1
fi
