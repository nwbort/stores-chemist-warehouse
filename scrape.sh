#!/usr/bin/env bash
#
# The entrypoint the workflow calls. Everything Chemist Warehouse-specific
# lives in scrape.py; this only exists so the workflow stays the same shape as
# every other stores-* repo.
#
#   ./scrape.sh              full national sweep (~7,700 requests, ~15 min)
#   ./scrape.sh --targeted   sweep around the stores already in stores.json
#   ./scrape.sh --check      lattice/coverage checks only, no requests
#
# SWEEP=targeted ./scrape.sh is equivalent to --targeted, which is how the
# workflow picks a mode.

set -euo pipefail

cd "$(dirname "$0")"

if [ "${1:-}" = "--check" ]; then
  exec python3 tests/check_coverage.py
fi

# The geometry is cheap to verify and expensive to get wrong, so it runs before
# every scrape rather than only in CI.
python3 tests/check_coverage.py >/dev/null

exec python3 scrape.py "$@"
