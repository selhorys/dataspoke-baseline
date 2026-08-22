#!/usr/bin/env bash
# Pre-flight checks for prauto heartbeat runs.
# Exit code 0 = all checks passed, non-zero = at least one failed.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
PRAUTO_DIR="${REPO_DIR}/.prauto"

ok=0
fail=0

pass() { echo "  [OK]  $1"; ok=$((ok + 1)); }
fail() { echo "  [FAIL] $1"; fail=$((fail + 1)); }

echo "Prauto pre-flight checks"
echo "========================"

# 1. config.local.env exists (do NOT read contents — contains secrets)
if [[ -f "${PRAUTO_DIR}/config.local.env" ]]; then
  pass "config.local.env exists"
else
  fail "config.local.env missing — create from config.local.env.example"
fi

# 2. heartbeat.sh exists and is executable
if [[ -x "${PRAUTO_DIR}/heartbeat.sh" ]]; then
  pass "heartbeat.sh is executable"
elif [[ -f "${PRAUTO_DIR}/heartbeat.sh" ]]; then
  fail "heartbeat.sh exists but is not executable (chmod +x)"
else
  fail "heartbeat.sh not found"
fi

# 3. Stale lock check
if [[ -f "${PRAUTO_DIR}/state/heartbeat.lock" ]]; then
  lock_pid=$(cat "${PRAUTO_DIR}/state/heartbeat.lock" 2>/dev/null || echo "")
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    fail "heartbeat.lock held by running process (PID ${lock_pid})"
  else
    fail "heartbeat.lock is stale (PID ${lock_pid} not running) — remove manually or let heartbeat auto-clean"
  fi
else
  pass "No stale lock"
fi

# 4. State directories
mkdir -p "${PRAUTO_DIR}/state/sessions" "${PRAUTO_DIR}/worktrees" 2>/dev/null
pass "State directories exist"

# 5. Required CLI tools
missing_tools=()
for tool in claude gh git jq; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    missing_tools+=("$tool")
  fi
done
if [[ ${#missing_tools[@]} -eq 0 ]]; then
  pass "Required tools: claude, gh, git, jq"
else
  fail "Missing tools: ${missing_tools[*]}"
fi

echo ""
echo "Result: ${ok} passed, ${fail} failed"
exit "$fail"
