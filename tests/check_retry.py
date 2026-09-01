#!/usr/bin/env python3
"""Retry and rate-limit behaviour, against a fake API.

This file exists because the first CI run failed in exactly this code and none
of it had ever been executed: eight workers each burned five retries against a
shared rate limit in 27 seconds. The second run then ground for half an hour,
because the throttle that replaced it could only ever slow down.

None of this can be exercised against the real endpoint - from a normal
connection it will not 429 even under a deliberate 600-request burst at 40
workers - so it gets a fake. It runs before every scrape, so it also has to be
quick: the pacing constants are shrunk to keep the whole file well under a
second, which is why they are set per-section rather than once at the top.
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


def http_error(code):
    return urllib.error.HTTPError("https://example.invalid", code, "nope",
                                  email.message.Message(), None)


def headers_with(retry_after):
    message = email.message.Message()
    message["Retry-After"] = retry_after
    return message


original_request_page = scrape.request_page


# =============================================================================
# The pacing arithmetic. No network path, so no waiting - these are instant.
# =============================================================================

throttle = scrape.Throttle()
opened_at = throttle.interval
for _ in range(3):
    throttle.penalise()
check(throttle.interval > opened_at,
      f"a 429 widens the interval ({opened_at:.3f}s -> {throttle.interval:.3f}s)")
check(throttle.penalties == 3, f"every 429 is counted ({throttle.penalties})")

# The regression behind the 30-minute second run: a throttle that only ever
# slows down holds every remaining point of a sweep at the slowest pace it has
# ever seen. It has to ease back off when requests start landing again.
slowed = throttle.interval
throttle.succeeded()
check(throttle.interval < slowed,
      f"one success eases the interval back ({slowed:.3f}s -> {throttle.interval:.3f}s)")
for _ in range(200):
    throttle.succeeded()
check(throttle.interval == scrape.MIN_INTERVAL_S,
      f"sustained success returns to the floor ({scrape.MIN_INTERVAL_S}s)")

check(scrape.Throttle().interval == scrape.START_INTERVAL_S > scrape.MIN_INTERVAL_S,
      f"a sweep opens gently at {scrape.START_INTERVAL_S}s rather than at the floor")

# A 429 must cost exactly one pause. An earlier version pushed the shared
# deadline in penalise() *and* slept the same amount in the caller, so every
# pause was served roughly twice and the two compounded on each retry.
scrape.PAUSE_BASE_S, scrape.MAX_PAUSE_S = 0.3, 0.3
solo = scrape.Throttle()
pause, _, _ = solo.penalise()
began = time.monotonic()
solo.wait()
waited = time.monotonic() - began
check(waited <= pause * 1.35,
      f"one 429 costs one pause: asked {pause:.2f}s, wait() took {waited:.2f}s")

# --- Retry-After, in both forms ----------------------------------------------
scrape.MAX_PAUSE_S = 120.0
check(scrape.retry_after_seconds(headers_with("7")) == 7.0,
      "Retry-After: delta-seconds is parsed")
soon = email.utils.format_datetime(
    datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30))
parsed = scrape.retry_after_seconds(headers_with(soon))
check(parsed is not None and 25 <= parsed <= 31,
      f"Retry-After: HTTP-date is parsed ({parsed:.0f}s)")
check(scrape.retry_after_seconds(headers_with("not-a-date")) is None,
      "an unparseable Retry-After falls back to our own backoff")
check(scrape.retry_after_seconds(headers_with("99999")) == scrape.MAX_PAUSE_S,
      f"an absurd Retry-After is capped at {scrape.MAX_PAUSE_S:g}s, not obeyed")

# Retry-After is a floor on our backoff, never a replacement for it. This is
# the bug that made the second CI run retry flat out for half an hour:
# Cloudflare answers a blocked address with a Retry-After that parses as zero,
# and obeying it literally means no backoff at all.
scrape.PAUSE_BASE_S, scrape.MAX_PAUSE_S = 4.0, 120.0
zero_pause, _, _ = scrape.Throttle().penalise(0.0)
check(zero_pause >= scrape.PAUSE_BASE_S * 0.5,
      f"Retry-After: 0 does not defeat our own backoff (paused {zero_pause:.1f}s)")
longer, _, _ = scrape.Throttle().penalise(90.0)
check(longer >= 90.0, f"a Retry-After longer than our backoff is honoured ({longer:.0f}s)")


# =============================================================================
# The retry loop, driving fetch_point against a fake. Pacing is pinned flat so
# the test measures behaviour rather than sleeping through the real backoff.
# =============================================================================

scrape.START_INTERVAL_S = scrape.MIN_INTERVAL_S = scrape.MAX_INTERVAL_S = 0.0
scrape.PAUSE_BASE_S = scrape.MAX_PAUSE_S = 0.0

calls = {"n": 0}


def flaky(lat, lon, offset, throttle=None):
    if throttle:
        throttle.wait()
    calls["n"] += 1
    if calls["n"] <= 3:
        raise http_error(429)
    return {"channels": [{"channel": {"key": "cwr-cw-au-store-1"}}]}


scrape.request_page = flaky
result = scrape.fetch_point(-33.87, 151.21, scrape.Throttle(0.0))
check(len(result) == 1 and calls["n"] == 4,
      f"three 429s then success: recovered after {calls['n']} calls")


def always_limited(lat, lon, offset, throttle=None):
    if throttle:
        throttle.wait()
    raise http_error(429)


scrape.request_page = always_limited
try:
    scrape.fetch_point(-33.87, 151.21, scrape.Throttle(0.0))
    check(False, "persistent 429 raises FetchError")
except scrape.FetchError as exc:
    check("rate limited" in str(exc) and "shared IP" in str(exc),
          "persistent 429 raises FetchError naming the likely cause")

hits = {"n": 0}


def bad_request(lat, lon, offset, throttle=None):
    hits["n"] += 1
    raise urllib.error.HTTPError("https://example.invalid", 400, "bad", None, None)


scrape.request_page = bad_request
try:
    scrape.fetch_point(-33.87, 151.21, scrape.Throttle(0.0))
    check(False, "a 400 raises FetchError")
except scrape.FetchError:
    check(hits["n"] == 1, f"a 400 is surfaced on the first try, not retried ({hits['n']} call)")

# An address that is refused outright rather than throttled has to fail fast.
# The second CI run ground for 29 minutes without one request getting through;
# there is no pace to discover in that state, so say so and stop.
patient = scrape.RATE_LIMIT_ATTEMPTS
scrape.RATE_LIMIT_ATTEMPTS = 10 ** 6
scrape.request_page = always_limited
began = time.monotonic()
try:
    scrape.fetch_point(-33.87, 151.21, scrape.Throttle(0.0))
    check(False, "a blocked address aborts the sweep")
except scrape.FetchError as exc:
    check("refused" in str(exc) and time.monotonic() - began < 1.0,
          f"{scrape.BLOCKED_AFTER} 429s with no success aborts in "
          f"{time.monotonic() - began:.2f}s rather than grinding")
scrape.RATE_LIMIT_ATTEMPTS = patient


# =============================================================================
# THE regression: one worker's 429 has to hold up every other worker. This is
# what the first CI failure was actually about.
# =============================================================================

scrape.PAUSE_BASE_S = scrape.MAX_PAUSE_S = 0.3
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
scrape.sweep({(row, 0) for row in range(16)})
gap = min(observed) - start if observed else 0.0
check(gap >= 0.15,
      f"a single 429 held the whole pool back {gap:.2f}s "
      f"(before the fix, the other workers carried straight on)")

scrape.request_page = original_request_page
print()
if failures:
    sys.exit(f"{len(failures)} check(s) failed")
print("all checks passed")
