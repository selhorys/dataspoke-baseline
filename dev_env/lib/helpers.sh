# Shared shell helpers for dev_env scripts.
# Source this file — do not execute directly.
# Usage: source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/helpers.sh"
#   (adjust the relative path depending on script depth)

info()  { echo -e "\033[0;32m[INFO]\033[0m  $*"; }
warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error() { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

# Run kubectl port-forward in a restart loop with keepalive.
# Usage: port_forward_loop <namespace> <target> <local_port>:<remote_port>
# Prints the wrapper-loop PID to stdout.
port_forward_loop() {
  local ns="$1" target="$2" ports="$3"
  (
    trap 'kill $PF_PID 2>/dev/null; exit 0' TERM INT
    while true; do
      kubectl port-forward --namespace "$ns" "$target" "$ports" \
        >/dev/null 2>&1 &
      PF_PID=$!
      wait $PF_PID || true
      sleep 2
    done
  ) >/dev/null 2>&1 &
  echo $!
}
