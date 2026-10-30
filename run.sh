#!/usr/bin/env bash
# Entry point for the scheduled tasks and for manual runs.
#
#   ./run.sh                one recording round (refused after freeze time)
#   ./run.sh --final        the 6:30 round: records everything and writes data/freeze.csv
#   ./run.sh --config FILE  use another config (tests use tests/config.test.json)
#
# Uses only git, gh and the system Python 3.9; never checks out or runs team code.
set -euo pipefail

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$DIR"

# Scheduled tasks and cron do not reliably carry PATH or USER.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export USER="${USER:-$(id -un)}"
# A private or deleted repo must fail at once, not wait for a password.
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/usr/bin/true
# Ignore this Mac's git config, above all the keychain login: a repo counts as reachable only
# if anyone could clone it. The GitHub API still uses gh's own login.
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null

mkdir -p "$DIR/data/logs"
LOG="$DIR/data/logs/run-$(date +%Y%m%dT%H%M%S).log"
/usr/bin/python3 "$DIR/scripts/record.py" "$@" 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
