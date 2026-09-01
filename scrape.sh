#!/usr/bin/env bash
#
# The entrypoint the workflow calls. Everything Chemist Warehouse-specific
# lives in scrape.py; this only exists so the workflow stays the same shape as
# every other stores-* repo.
#
#   ./scrape.sh              full national sweep (~7,700 requests, ~15 min)
#   ./scrape.sh --targeted   sweep around the stores already in stores.json
#   ./scrape.sh --check      offline checks only (geometry + retry), no requests
#
# SWEEP=targeted ./scrape.sh is equivalent to --targeted, which is how the
# workflow picks a mode.

set -euo pipefail

cd "$(dirname "$0")"

run_checks() {
  python3 tests/check_coverage.py "$@"
  python3 tests/check_retry.py "$@"
}

if [ "${1:-}" = "--check" ]; then
  run_checks
  exit 0
fi

# Both suites are seconds of work and neither touches the network, so they run
# before every scrape rather than only in CI. The retry suite in particular
# guards code that only ever executes when something has already gone wrong.
run_checks >/dev/null

exec python3 scrape.py "$@"
