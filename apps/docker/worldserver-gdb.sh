#!/usr/bin/env bash
set -euo pipefail

# Launches the worldserver under gdb in batch mode so that, on a fatal signal
# (SIGSEGV/SIGABRT/SIGBUS/...), gdb writes a human-readable backtrace to a
# timestamped file in the persisted logs volume. Enabled opt-in via the
# entrypoint when GDB_ENABLED=1. Mirrors apps/startup-scripts/src/starter.

CRASHES_DIR="${AC_LOGS_DIR:-/azerothcore/env/dist/logs}/crashes"
mkdir -p "$CRASHES_DIR"

TIMESTAMP="$(date +%Y-%m-%d-%H-%M-%S)"
GDB_OUTPUT_FILE="$CRASHES_DIR/gdb-$TIMESTAMP.txt"
GDB_CMD_FILE="$(mktemp)"

# Clean up the generated command file on exit
trap 'rm -f "$GDB_CMD_FILE"' EXIT

BINARY="$(command -v worldserver)"
if [ -z "$BINARY" ]; then
  echo "Error: worldserver binary not found on PATH" >&2
  exit 1
fi

# Build the gdb command file.
#
# - Logging captures everything gdb prints (including the backtraces below)
#   into the crash file.
# - SIGTERM/SIGINT/SIGPIPE are passed straight through to the inferior so that
#   `docker stop` (SIGTERM) shuts the server down cleanly instead of trapping
#   in gdb. SIGSEGV/SIGABRT/SIGBUS keep gdb's default behaviour (stop), which
#   is what triggers the backtrace dump.
# - On stop we emit full backtraces for every thread, then kill the inferior
#   and quit so the container exits and `restart: unless-stopped` recycles it.
cat > "$GDB_CMD_FILE" << EOF
set pagination off
set logging file $GDB_OUTPUT_FILE
set logging enabled on
set debug timestamp
handle SIGTERM nostop noprint pass
handle SIGINT nostop noprint pass
handle SIGPIPE nostop noprint pass
run
bt
bt full
info threads
thread apply all backtrace full
kill
quit
EOF

echo "Starting worldserver under gdb (crash backtraces -> $GDB_OUTPUT_FILE)"
exec gdb -x "$GDB_CMD_FILE" --batch "$BINARY"
