#!/usr/bin/env python3
"""Retry and rate-limit behaviour, against a fake API.

This file exists because the first CI run failed in exactly this code and no
test had ever run it: eight workers each burned five retries against a shared
rate limit in 27 seconds. None of it can be exercised against the real
endpoint - from a normal connection it will not 429 even under a deliberate
600-request burst at 40 workers - so it gets a fake.
"""

import datetime
import email.message
import email.utils
import os
import sys
import threading
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scrape  # noqa: E402

failures = []


def check(condition, message):
    print(f"{'PASS' if condition else 'FAIL'}  {message}")
    if not condition:
        failures.append(message)


def http_error(code, headers=None):
    return urllib.error.HTTPError(
        "https://example.invalid", code, "rate limited",
        email.message.Message() if headers is None else headers, None)


def headers_with(retry_after):
    message = email.message.Message()
    message["Retry-After"] = retry_after
    return message


# Keep the tests quick - the real pauses are 5s and up.
scrape.PAUSE_BASE_S = 0.02
scrape.MAX_PAUSE_S = 0.5

original_request_page = scrape.request_page


# --- 1. a transient 429 is survived, not fatal --------------------------------
calls = {"n": 0}


def flaky(lat, lon, offset, throttle=None):
    if throttle:
        throttle.wait()
    calls["n"] += 1
    if calls["n"] <= 3:
        raise http_error(429)
    return {"channels": [{"channel": {"key": "cwr-cw-au-store-1"}}]}


scrape.request_page = flaky
throttle = scrape.Throttle()
result = scrape.fetch_point(-33.87, 151.21, throttle)
check(len(result) == 1 and calls["n"] == 4,
      f"three 429s then success: recovered after {calls['n']} calls")
check(throttle.penalties == 3, f"each 429 penalised the throttle ({throttle.penalties})")

# --- 2. the run paces itself down after being limited -------------------------
check(throttle._interval > scrape.REQUEST_INTERVAL_S,
      f"interval widened from {scrape.REQUEST_INTERVAL_S}s to {throttle._interval:.3f}s")

# --- 3. persistent 429 gives up, with an actionable message -------------------
def always_limited(lat, lon, offset, throttle=None):
    if throttle:
        throttle.wait()
    raise http_error(429)


scrape.request_page = always_limited
try:
    scrape.fetch_point(-33.87, 151.21, scrape.Throttle())
    check(False, "persistent 429 raises FetchError")
except scrape.FetchError as exc:
    check("rate limited" in str(exc) and "shared IP" in str(exc),
          "persistent 429 raises FetchError naming the likely cause")

# --- 4. a 4xx that is not 429 is surfaced immediately, not retried ------------
hits = {"n": 0}


def bad_request(lat, lon, offset, throttle=None):
    hits["n"] += 1
    raise urllib.error.HTTPError("https://example.invalid", 400, "bad", None, None)


scrape.request_page = bad_request
try:
    scrape.fetch_point(-33.87, 151.21, scrape.Throttle())
    check(False, "a 400 raises FetchError")
except scrape.FetchError:
    check(hits["n"] == 1, f"a 400 is surfaced on the first try, not retried ({hits['n']} call)")
except Exception as exc:  # pragma: no cover
    check(False, f"a 400 raised {type(exc).__name__} rather than FetchError")

# --- 5. Retry-After is honoured, in both forms --------------------------------
# Back to the real cap: the checks above deliberately shrank it for speed, and
# it clamps parsed values.
scrape.MAX_PAUSE_S = 120.0
check(scrape.retry_after_seconds(headers_with("7")) == 7.0,
      "Retry-After: delta-seconds is parsed")
soon = email.utils.format_datetime(
    datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30))
parsed = scrape.retry_after_seconds(headers_with(soon))
check(parsed is not None and 25 <= parsed <= 31, f"Retry-After: HTTP-date is parsed ({parsed:.0f}s)")
check(scrape.retry_after_seconds(headers_with("not-a-date")) is None,
      "an unparseable Retry-After falls back to our own backoff")
check(scrape.retry_after_seconds(headers_with("99999")) == scrape.MAX_PAUSE_S,
      f"an absurd Retry-After is capped at {scrape.MAX_PAUSE_S:g}s rather than hanging the run")

# --- 6. THE regression: one worker's 429 pauses every other worker -------------
# This is what the first CI failure was actually about.
scrape.PAUSE_BASE_S = 0.4
scrape.MAX_PAUSE_S = 2.0
shared = scrape.Throttle()
limited_once = {"done": False}
observed = []
lock = threading.Lock()


def limit_first_caller(lat, lon, offset, throttle=None):
    if throttle:
        throttle.wait()
    with lock:
        first = not limited_once["done"]
        limited_once["done"] = True
    if first:
        raise http_error(429)
    with lock:
        observed.append(time.monotonic())
    return {"channels": []}


scrape.request_page = limit_first_caller
start = time.monotonic()
scrape.sweep(set(list(scrape.full_sweep_cells())[:16]))
gap = min(observed) - start if observed else 0
check(gap >= 0.35,
      f"a single 429 held the whole pool back {gap:.2f}s "
      f"(before the fix, other workers carried straight on)")

scrape.request_page = original_request_page
print()
if failures:
    sys.exit(f"{len(failures)} check(s) failed")
print("all checks passed")
