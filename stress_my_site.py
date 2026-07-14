#!/usr/bin/env python3
"""stress_my_site.py - async HTTP load/stress tester.

Generates real concurrent HTTP load against a target you own or are
authorized to test, and reports throughput, latency percentiles, and the
approximate load level at which the target starts failing (the "breaking
point").

This is a genuine load generator (comparable to `ab`, `hey`, or `k6`), not a
browser-tab simulator: requests are fired directly over HTTP with asyncio +
aiohttp, so throughput is bounded by your machine's CPU/network, not by how
many browser tabs Chrome can render.

Three modes:
    break     Ramp concurrency up until the target starts failing, and
              report the breaking point.
    requests  Ramp concurrency up until measured throughput reaches
              --target-rps, then hold to confirm it's actually sustained.
    takedown  Ramp past the breaking point, then hold the target down for a
              fixed number of minutes - escalating concurrency whenever it
              recovers - so you can test your own defenses (rate limiting,
              autoscaling, alerting) in real time while it runs. Always
              stops automatically after --minutes; Ctrl+C stops it early.

Usage examples:
    # Find the breaking point, starting at 50 and ramping toward an
    # auto-picked ceiling (200x -c) over up to 300s:
    python stress_my_site.py break --url https://example.com -c 50

    # Same, but with an explicit ceiling and a tighter safety cap:
    python stress_my_site.py break --url http://localhost:8080 \
        -c 100 --max-concurrency 20000 -d 120

    # Ramp up until throughput reaches 500 req/s, then hold for 30s to
    # confirm it's sustained:
    python stress_my_site.py requests --url http://localhost:8080 \
        --target-rps 500

    # Break it, then hold it down for 5 minutes (escalating if it recovers)
    # so you can watch your alerting/autoscaling respond in real time:
    python stress_my_site.py takedown --url http://localhost:8080 -m 5

    python stress_my_site.py            # interactive prompts for everything

IMPORTANT: Only point this at systems you own or are explicitly authorized to
test. Sustained concurrent load can degrade or take down a service.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

# break mode: outer safety cap on total run time, in case the target never
# breaks (user-overridable via --duration).
DEFAULT_BREAK_DURATION = 300.0
# requests mode: how long to hold once the measured throughput reaches
# --target-rps, to confirm it's actually sustained rather than a one-second
# blip.
REQUESTS_MODE_HOLD_SECONDS = 30.0
# requests mode: how long to keep growing concurrency toward --target-rps
# before giving up (internal outer safety cap - not user-facing, mirrors
# break mode's --duration but requests mode has no reason to expose it since
# the ramp is throughput-driven, not time-driven).
REQUESTS_MODE_RAMP_SAFETY = 120.0
# Rough, deliberately conservative assumption used only to pick a sane
# default --max-concurrency in requests mode when the user doesn't set one
# explicitly: how many req/s a single worker can realistically sustain
# against a reasonably fast target. Real targets vary a lot, hence the 3x
# headroom on top in `default_requests_max_concurrency` - this is a starting
# guess, not a promise, and is always overridable with --max-concurrency.
ASSUMED_RPS_PER_WORKER = 50.0
# takedown mode: outer safety cap on the initial ramp-to-breaking-point
# phase, in case the target never breaks even at the concurrency ceiling
# (internal - not user-facing, mirrors requests mode's own ramp safety cap).
TAKEDOWN_RAMP_SAFETY = 120.0
# takedown mode: concurrency growth factor applied each time the target is
# observed to have recovered during the hold window (+25% per escalation,
# always at least +1 worker - see `escalate_concurrency`).
TAKEDOWN_ESCALATION_FACTOR = 1.25

try:
    import aiohttp
except ImportError:  # pragma: no cover - import-time guard, not test target
    print(
        "Missing dependency 'aiohttp'. Install with: pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)


# --------------------------------------------------------------------------
# Pure, unit-testable helpers (no network, no asyncio)
# --------------------------------------------------------------------------

def normalize_url(raw: str) -> str:
    """Prepend a scheme if the user typed a bare host/IP/host:port, trim whitespace.

    Examples:
        "example.com"        -> "https://example.com"
        "localhost:8080"      -> "https://localhost:8080"
        " http://10.0.0.5 "  -> "http://10.0.0.5"
        "https://example.com"-> "https://example.com"

    Note: `urlparse("localhost:8080")` misreads "localhost" as a URL scheme,
    so a scheme is detected here with an explicit "://" regex instead of
    relying on `urlparse().scheme` — otherwise the common host:port case
    would silently pass through without a scheme and fail in aiohttp.
    """
    url = raw.strip()
    if not url:
        raise ValueError("URL must not be empty")
    if not _SCHEME_RE.match(url):
        url = f"https://{url}"
    return url


def percentile(sorted_values: list[float], p: float) -> float:
    """Return the p-th percentile (0-100) of an already-sorted list.

    Uses linear interpolation between closest ranks (same approach as
    numpy's default). Raises ValueError on an empty list.
    """
    if not sorted_values:
        raise ValueError("cannot compute a percentile of an empty sequence")
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    if len(sorted_values) == 1:
        return sorted_values[0]

    rank = (p / 100) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def ramp_delays(concurrency: int, ramp_seconds: float) -> list[float]:
    """Return, per worker, the delay (seconds) before it starts sending requests.

    Workers are staggered linearly across `ramp_seconds` so load builds up
    gradually instead of slamming the target at full concurrency instantly.
    With ramp_seconds <= 0, every worker starts immediately (delay 0.0).
    """
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    if ramp_seconds <= 0:
        return [0.0] * concurrency
    if concurrency == 1:
        return [0.0]
    step = ramp_seconds / (concurrency - 1)
    return [i * step for i in range(concurrency)]


def ramp_delays_from_floor(floor: int, peak: int, ramp_seconds: float) -> list[float]:
    """Like `ramp_delays`, but the first `floor` workers start immediately
    (delay 0.0) and only the remaining `peak - floor` workers stagger in,
    reaching `peak` by `ramp_seconds`.

    Used for a continuous ramp toward `--max-concurrency`: the run should
    start at `-c`/`--concurrency` (the floor) rather than climbing from
    scratch, so `-c` keeps meaning "starting concurrency" instead of being
    silently ignored once a higher ceiling is in play.
    """
    if floor <= 0:
        raise ValueError("floor must be positive")
    if peak < floor:
        raise ValueError("peak must be >= floor")
    extra = peak - floor
    if extra == 0 or ramp_seconds <= 0:
        return [0.0] * peak
    step = ramp_seconds / extra
    return [0.0] * floor + [(i + 1) * step for i in range(extra)]


def default_break_max_concurrency(concurrency: int) -> int:
    """Default --max-concurrency ceiling for `break` mode when omitted.

    break mode's whole job is finding the ceiling, so it needs a generous
    one to climb toward rather than plateauing right at the starting
    concurrency. 200x mirrors the ratio used in this project's own
    documented example (100 -> 20,000).
    """
    return concurrency * 200


def default_requests_max_concurrency(concurrency: int, target_rps: float) -> int:
    """Default --max-concurrency ceiling for `requests` mode when omitted.

    A rough headroom estimate, not a promise: assumes each worker can
    sustain about `ASSUMED_RPS_PER_WORKER` req/s, then triples that to leave
    room for a target that's slower than assumed. Always overridable with
    an explicit --max-concurrency.
    """
    estimated = math.ceil(target_rps / ASSUMED_RPS_PER_WORKER * 3)
    return max(concurrency, estimated)


def next_concurrency(current: int, measured_rps: float, target_rps: float, ceiling: int) -> int:
    """Compute the next worker count while ramping toward `target_rps`.

    Grows proportionally to how far `measured_rps` (the most recently
    closed bucket's throughput) is from `target_rps`, capped at doubling in
    a single step to avoid wildly overshooting on a noisy first sample, and
    always advancing by at least one worker so a flat `measured_rps` (e.g.
    0 during the very first second) still makes forward progress. Clamped
    to `ceiling` (--max-concurrency).

    Callers should only invoke this while still under target; once
    `measured_rps >= target_rps` the caller should freeze concurrency
    instead of calling this.
    """
    if current <= 0:
        raise ValueError("current must be positive")
    if ceiling < current:
        raise ValueError("ceiling must be >= current")
    if measured_rps <= 0:
        candidate = current * 2
    else:
        ratio = min(target_rps / measured_rps, 2.0)
        candidate = math.ceil(current * ratio)
    candidate = max(candidate, current + 1)
    return min(candidate, ceiling)


def escalate_concurrency(current: int, ceiling: Optional[int] = None, factor: float = TAKEDOWN_ESCALATION_FACTOR) -> int:
    """Compute the next worker count for `takedown` mode's hold phase.

    Called each time the target is observed to have recovered while held
    down: grows concurrency by `factor` (default +25%) to push past
    whatever recovery just happened, always advancing by at least one
    worker so a small `current` still makes forward progress. Clamped to
    `ceiling` (--max-concurrency) if one was given - once there, further
    recoveries are reported but can no longer be escalated against.
    `ceiling=None` (the default: no --max-concurrency was given) means
    unbounded - escalation is limited only by this machine's own resources,
    not by a guessed number.
    """
    if current <= 0:
        raise ValueError("current must be positive")
    if ceiling is not None and ceiling < current:
        raise ValueError("ceiling must be >= current")
    candidate = max(math.ceil(current * factor), current + 1)
    return candidate if ceiling is None else min(candidate, ceiling)


@dataclass
class Bucket:
    """Aggregated stats for one time window (default: 1 second) of the run.

    A result is bucketed by the second in which its request *started*
    (relative to the run's start time), once it completes. `active_load` is
    the number of worker coroutines concurrently in flight at that moment —
    not the same as the bucket's realized req/s, which can differ (e.g. under
    an --rps cap or when the target itself is slow to respond).
    """

    index: int
    requests: int = 0
    successes: int = 0
    errors: int = 0  # any non-2xx/3xx response or request exception
    timeouts: int = 0
    server_errors: int = 0  # HTTP 5xx
    hard_failures: int = 0  # timeouts + 5xx + connection/network errors (excludes 4xx)
    active_load: int = 0  # concurrency active during this bucket
    latencies: list[float] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0

    @property
    def hard_failure_rate(self) -> float:
        """Failure rate used for breaking-point detection.

        Deliberately excludes 4xx responses: a target returning 401/403/404
        under load is answering as designed, not failing under load, so
        counting those toward the threshold would produce false breaking-point
        positives against endpoints that legitimately reject requests.
        """
        return self.hard_failures / self.requests if self.requests else 0.0

    @property
    def p95_latency(self) -> Optional[float]:
        if not self.latencies:
            return None
        return percentile(sorted(self.latencies), 95)


@dataclass
class BreakingPoint:
    bucket_index: int
    active_load: int
    failure_rate: float
    p95_latency: Optional[float]
    reason: str


@dataclass
class RequestsModeOutcome:
    """Result of a `requests` mode run: did it reach --target-rps, and did
    that rate hold up for the full hold window once reached?"""

    target_rps: float
    reached: bool
    concurrency_at_target: Optional[int] = None
    measured_rps_at_target: Optional[float] = None
    sustained: Optional[bool] = None  # None means the target was never reached, so holding never started


@dataclass
class TakedownOutcome:
    """Result of a `takedown` mode run: did the target break during the
    ramp, and how did it behave while held down for the configured window?
    """

    hold_minutes: float
    breaking_point_found: bool
    concurrency_at_break: Optional[int] = None
    escalations: int = 0  # number of times the target recovered and concurrency was pushed higher
    final_concurrency: Optional[int] = None


def find_breaking_point(
    buckets: list[Bucket],
    err_threshold: float = 0.05,
    latency_factor: float = 3.0,
    sustained_buckets: int = 2,
    stall_factor: float = 2.0,
) -> Optional[BreakingPoint]:
    """Find the first sustained window where the target starts failing.

    A window is considered a breaking point if, for `sustained_buckets`
    consecutive buckets:
      - the hard-failure rate (timeouts + 5xx + connection errors — 4xx is
        deliberately excluded, see `Bucket.hard_failure_rate`) exceeds
        `err_threshold`, OR
      - the p95 latency exceeds `latency_factor` times the baseline p95
        (the p95 of the first bucket that has data), OR
      - the target has gone quiet: zero requests *completed* in a second
        where workers were known to be active (`active_load > 0`), for
        longer than `max(baseline_p95 * stall_factor, 1.0)` seconds, *once a
        baseline p95 exists* (i.e. at least one request has completed
        somewhere in the run). That floor matters — a target with a 3s
        baseline latency can legitimately produce a couple of empty
        one-second buckets between completions while still being healthy,
        so a bare "any empty second" rule would false-positive on
        slow-but-fine targets. Gauging the gap against the baseline latency
        instead only flags a *real* stall: workers in flight noticeably
        longer than they normally take. The baseline requirement also
        matters on its own: before anything has completed even once, the
        threshold would otherwise collapse to its 1.0s floor and misfire
        against any target simply slower than that at cold start. A stall
        from the very first request is instead caught by each worker's own
        `--timeout` producing a hard failure once it fires.

    A completely stalled window (all workers wedged, nothing completing)
    used to be invisible to this function entirely: buckets with zero
    completions were dropped before any of the checks above ran, so a full
    stall could never accumulate `sustained_buckets` in a row. Buckets are
    now kept as long as workers were active during them (`active_load > 0`),
    so the stall itself becomes the detected failure.

    Returns None if no such sustained degradation is found.
    """
    data_buckets = [b for b in buckets if b.requests > 0 or b.active_load > 0]
    if not data_buckets:
        return None

    baseline_p95 = next((b.p95_latency for b in data_buckets if b.p95_latency is not None), None) or 0.0
    stall_threshold = max(baseline_p95 * stall_factor, 1.0)

    def is_bad(b: Bucket, gap_seconds: int) -> tuple[bool, str]:
        if b.requests == 0:
            # No baseline latency yet means nothing has completed *at all*
            # so far in this run - that's indistinguishable from a target
            # that's simply slow but healthy and still mid-first-request
            # (e.g. a multi-second-latency target right at cold start). A
            # real stall from the very first request is instead caught by
            # each worker's own --timeout producing a hard failure once it
            # fires, just not as immediately as this check would otherwise
            # claim.
            if baseline_p95 > 0 and gap_seconds > stall_threshold:
                return True, (
                    f"no requests completed for {gap_seconds}s while "
                    f"{b.active_load} workers were active (stalled)"
                )
            return False, ""
        if b.hard_failure_rate > err_threshold:
            return True, f"failure rate {b.hard_failure_rate:.1%} exceeded {err_threshold:.0%} threshold"
        if baseline_p95 > 0 and b.p95_latency is not None and b.p95_latency > baseline_p95 * latency_factor:
            return True, f"p95 latency {b.p95_latency:.3f}s exceeded {latency_factor}x baseline ({baseline_p95:.3f}s)"
        return False, ""

    # Evaluated up front (rather than re-derived from the bucket alone once a
    # sustained run is found) because `is_bad` for a zero-request bucket
    # depends on `gap_seconds`, which is state accumulated while scanning
    # forward, not something recoverable from a single bucket in isolation.
    evaluations: list[tuple[bool, str]] = []
    gap_seconds = 0
    for b in data_buckets:
        gap_seconds = 0 if b.requests > 0 else gap_seconds + 1
        evaluations.append(is_bad(b, gap_seconds))

    consecutive = 0
    for position, (bad, _reason) in enumerate(evaluations):
        if bad:
            consecutive += 1
            if consecutive >= sustained_buckets:
                first_bad_position = max(position - sustained_buckets + 1, 0)
                first_bad = data_buckets[first_bad_position]
                _, first_reason = evaluations[first_bad_position]
                return BreakingPoint(
                    bucket_index=first_bad.index,
                    active_load=first_bad.active_load,
                    failure_rate=first_bad.hard_failure_rate,
                    p95_latency=first_bad.p95_latency,
                    reason=first_reason,
                )
        else:
            consecutive = 0
    return None


@dataclass
class RunStats:
    url: str
    total_requests: int = 0
    successes: int = 0
    errors: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)
    error_counts: dict[str, int] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    duration: float = 0.0
    buckets: list[Bucket] = field(default_factory=list)


def build_report(
    stats: RunStats,
    breaking_point: Optional[BreakingPoint],
    requests_outcome: Optional[RequestsModeOutcome] = None,
    takedown_outcome: Optional[TakedownOutcome] = None,
) -> str:
    """Format the final human-readable report from raw run statistics."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"Target:            {stats.url}")
    lines.append(f"Duration:          {stats.duration:.2f}s")
    lines.append(f"Total requests:    {stats.total_requests}")
    if stats.total_requests:
        success_rate = stats.successes / stats.total_requests
        lines.append(f"Successful:        {stats.successes} ({success_rate:.1%})")
        lines.append(f"Failed:            {stats.errors} ({1 - success_rate:.1%})")
    if stats.duration > 0:
        lines.append(f"Throughput:        {stats.total_requests / stats.duration:.1f} req/s")

    if stats.status_counts:
        lines.append("")
        lines.append("Status codes:")
        for code in sorted(stats.status_counts):
            lines.append(f"  {code}: {stats.status_counts[code]}")

    if stats.error_counts:
        lines.append("")
        lines.append("Errors:")
        for kind in sorted(stats.error_counts):
            lines.append(f"  {kind}: {stats.error_counts[kind]}")

    if stats.latencies:
        sorted_lat = sorted(stats.latencies)
        lines.append("")
        lines.append("Latency (seconds):")
        lines.append(f"  min:  {sorted_lat[0]:.4f}")
        lines.append(f"  avg:  {statistics.fmean(sorted_lat):.4f}")
        lines.append(f"  p50:  {percentile(sorted_lat, 50):.4f}")
        lines.append(f"  p90:  {percentile(sorted_lat, 90):.4f}")
        lines.append(f"  p95:  {percentile(sorted_lat, 95):.4f}")
        lines.append(f"  p99:  {percentile(sorted_lat, 99):.4f}")
        lines.append(f"  max:  {sorted_lat[-1]:.4f}")

    if stats.buckets:
        lines.append("")
        lines.append("Per-second breakdown (time / active load / req/s / success rate / p95):")
        for b in stats.buckets:
            if b.requests == 0 and b.active_load == 0:
                continue
            success_rate = b.successes / b.requests if b.requests else 0.0
            p95 = f"{b.p95_latency:.3f}s" if b.p95_latency is not None else "n/a"
            success_display = f"{success_rate:>6.1%}" if b.requests else "  stall"
            lines.append(
                f"  t={b.index:>4}s  load={b.active_load:>5}  "
                f"req/s={b.requests:>6}  success={success_display}  p95={p95}"
            )

    if requests_outcome is not None:
        lines.append("")
        lines.append(f"Target rate:       {requests_outcome.target_rps:.1f} req/s")
        if not requests_outcome.reached:
            lines.append(
                f"NOT REACHED - throughput never reached {requests_outcome.target_rps:.1f} req/s "
                f"even at the concurrency ceiling."
            )
        else:
            lines.append(
                f"Reached:           ~{requests_outcome.measured_rps_at_target:.1f} req/s "
                f"at concurrency {requests_outcome.concurrency_at_target}"
            )
            if requests_outcome.sustained:
                lines.append(
                    f"Sustained for the full {REQUESTS_MODE_HOLD_SECONDS:.0f}s hold window."
                )
            else:
                lines.append(
                    f"NOT SUSTAINED - the target rate degraded during the "
                    f"{REQUESTS_MODE_HOLD_SECONDS:.0f}s hold window (see breaking point below)."
                )

    if takedown_outcome is not None:
        lines.append("")
        if not takedown_outcome.breaking_point_found:
            lines.append(
                "Target never broke, even at the concurrency ceiling - held up under "
                "the full ramp, no hold window was run."
            )
        else:
            lines.append(
                f"Broke at concurrency ~{takedown_outcome.concurrency_at_break}, "
                f"held down for {takedown_outcome.hold_minutes:.1f} minute(s)."
            )
            lines.append(f"Recoveries observed (each triggered an escalation): {takedown_outcome.escalations}")
            lines.append(f"Final concurrency:  {takedown_outcome.final_concurrency}")

    lines.append("")
    if breaking_point is not None:
        lines.append("BREAKING POINT DETECTED:")
        lines.append(
            f"  Server started failing around t={breaking_point.bucket_index}s, "
            f"active load ~{breaking_point.active_load}."
        )
        lines.append(f"  Reason: {breaking_point.reason}")
        lines.append(
            f"  Failure rate at that point: {breaking_point.failure_rate:.1%}"
            + (f", p95 latency: {breaking_point.p95_latency:.3f}s" if breaking_point.p95_latency else "")
        )
        # `error_counts` holds only exceptions (timeouts, connection errors) -
        # actual server 5xx responses land in `status_counts` instead. If
        # non-timeout exceptions dominate over real 5xx responses, the
        # "failure" more likely reflects the client running out of its own
        # resources (ephemeral ports, file descriptors) than the target
        # actually breaking - most relevant at very high --max-concurrency.
        connection_errors = sum(count for kind, count in stats.error_counts.items() if kind != "timeout")
        server_5xx = sum(count for code, count in stats.status_counts.items() if code >= 500)
        if connection_errors > 0 and connection_errors > server_5xx:
            lines.append(
                f"  Caveat: {connection_errors} client-side connection errors vs. "
                f"{server_5xx} server 5xx responses - this may reflect the client "
                "(ephemeral ports, open file descriptors) hitting its own limit "
                "rather than the target actually failing. See README's 'A note "
                "on limits'."
            )
    else:
        lines.append("No breaking point detected - target held up under the applied load.")
    lines.append("=" * 60)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Load generation (asyncio + aiohttp)
# --------------------------------------------------------------------------

class TokenBucketLimiter:
    """A capped token-bucket rate limiter.

    Unlike releasing an uncapped `asyncio.Semaphore` on a timer, this never
    lets unconsumed permits accumulate past `capacity` — if workers stall
    (e.g. the target is slow) and then recover, they get gated back to the
    configured rate instead of bursting through a backlog of saved-up tokens.
    """

    def __init__(self, rps: float, capacity: Optional[float] = None) -> None:
        self.rps = rps
        self.capacity = capacity if capacity is not None else max(1.0, rps)
        self._tokens = self.capacity
        self._last = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rps)
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await asyncio.sleep((1 - self._tokens) / self.rps)


@dataclass
class RunConfig:
    mode: str  # "break", "requests", or "takedown"
    url: str
    concurrency: int  # ramp floor
    # Ramp/escalation ceiling. Always set (auto-defaulted if omitted) for
    # break/requests. For takedown it may be None - unbounded escalation,
    # capped only by this machine's own resources rather than a guessed
    # number, unless the user explicitly passes --max-concurrency.
    max_concurrency: Optional[int]
    method: str = "GET"
    timeout: float = 10.0
    verify_tls: bool = True
    duration: float = DEFAULT_BREAK_DURATION  # break: user-facing safety cap; requests/takedown: internal safety cap
    ramp_up: float = 0.0  # break/takedown modes only, internally computed - not user-facing
    rps: Optional[float] = None  # break mode only: optional global rate limit
    target_rps: Optional[float] = None  # requests mode only
    takedown_minutes: Optional[float] = None  # takedown mode only: how long to hold the target down once it breaks


class LoadGenerator:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.stats = RunStats(url=config.url)
        self._deadline: Optional[float] = None
        self._start_time = 0.0
        self._stop_event = asyncio.Event()
        self._rate_limiter = TokenBucketLimiter(config.rps) if config.rps else None
        self._active_workers = 0
        self.requests_outcome: Optional[RequestsModeOutcome] = None
        self.takedown_outcome: Optional[TakedownOutcome] = None

    @property
    def start_time(self) -> float:
        return self._start_time

    def _bucket_for(self, elapsed: float) -> Bucket:
        index = int(elapsed)
        while len(self.stats.buckets) <= index:
            self.stats.buckets.append(Bucket(index=len(self.stats.buckets)))
        return self.stats.buckets[index]

    def _last_full_bucket_rps(self) -> float:
        """Throughput of the most recently *closed* one-second bucket.

        The bucket for the current second is still filling, so it's not a
        reliable throughput reading yet - the previous one is the latest
        complete sample.
        """
        elapsed = time.monotonic() - self._start_time
        idx = int(elapsed) - 1
        if idx < 0 or idx >= len(self.stats.buckets):
            return 0.0
        return float(self.stats.buckets[idx].requests)

    async def _claim_request(self) -> bool:
        """Return True if this worker may send one more request."""
        if self._stop_event.is_set():
            return False
        return time.monotonic() < self._deadline

    async def _worker(self, session: "aiohttp.ClientSession", start_delay: float) -> None:
        if start_delay > 0:
            await asyncio.sleep(start_delay)
        self._active_workers += 1
        try:
            while await self._claim_request():
                if self._rate_limiter is not None:
                    await self._rate_limiter.acquire()
                t0 = time.monotonic()
                elapsed = t0 - self._start_time
                bucket = self._bucket_for(elapsed)
                bucket.active_load = self._active_workers
                try:
                    async with session.request(self.config.method, self.config.url) as resp:
                        await resp.read()
                        latency = time.monotonic() - t0
                        self.stats.total_requests += 1
                        self.stats.latencies.append(latency)
                        self.stats.status_counts[resp.status] = (
                            self.stats.status_counts.get(resp.status, 0) + 1
                        )
                        bucket.requests += 1
                        bucket.latencies.append(latency)
                        if 200 <= resp.status < 400:
                            self.stats.successes += 1
                            bucket.successes += 1
                        else:
                            self.stats.errors += 1
                            bucket.errors += 1
                            if resp.status >= 500:
                                bucket.server_errors += 1
                                bucket.hard_failures += 1
                except asyncio.TimeoutError:
                    latency = time.monotonic() - t0
                    self.stats.total_requests += 1
                    self.stats.errors += 1
                    self.stats.error_counts["timeout"] = self.stats.error_counts.get("timeout", 0) + 1
                    bucket.requests += 1
                    bucket.errors += 1
                    bucket.timeouts += 1
                    bucket.hard_failures += 1
                except (aiohttp.ClientError, OSError) as exc:
                    self.stats.total_requests += 1
                    self.stats.errors += 1
                    kind = type(exc).__name__
                    self.stats.error_counts[kind] = self.stats.error_counts.get(kind, 0) + 1
                    bucket.requests += 1
                    bucket.errors += 1
                    bucket.hard_failures += 1
        finally:
            self._active_workers -= 1

    async def _heartbeat(self) -> None:
        """Record `active_load` into the current bucket once a second.

        `_worker` only touches a bucket's `active_load` when it *starts* a
        request. If every worker is wedged mid-request for a whole second
        (the exact moment a real stall is happening), no worker calls
        `_bucket_for` and that second's `active_load` would otherwise stay
        at the dataclass default of 0 — indistinguishable from "no load was
        applied yet". This task guarantees every second gets a true
        `active_load` sample regardless of whether anything completed.
        """
        while not self._stop_event.is_set():
            await asyncio.sleep(1.0)
            elapsed = time.monotonic() - self._start_time
            bucket = self._bucket_for(elapsed)
            bucket.active_load = max(bucket.active_load, self._active_workers)

    async def _progress_reporter(self) -> None:
        """break mode's driver loop: reports progress and stops the run as
        soon as `find_breaking_point` fires - break mode's whole purpose is
        finding that point, so stopping on it is always on, unlike the old
        opt-in --stop-on-break flag.
        """
        last_count = 0
        while not self._stop_event.is_set():
            await asyncio.sleep(1.0)
            current = self.stats.total_requests
            print(
                f"[progress] t={time.monotonic() - self._start_time:5.1f}s  "
                f"requests={current}  (+{current - last_count}/s)  "
                f"active={self._active_workers}  errors={self.stats.errors}",
                file=sys.stderr,
            )
            last_count = current
            bp = find_breaking_point(self.stats.buckets)
            if bp is not None:
                print(
                    f"[break] breaking point detected at t={bp.bucket_index}s "
                    f"(active load ~{bp.active_load}) - stopping early",
                    file=sys.stderr,
                )
                self._stop_event.set()
                return

    async def _run_break(self, session: "aiohttp.ClientSession") -> list:
        # The ramp starts at `concurrency` (the floor - the historical
        # meaning of `-c`) and climbs toward `max_concurrency`, which is
        # always set by this point (auto-defaulted in build_config_from_args
        # if the user didn't pass one) - unlike takedown, break mode has no
        # unbounded option.
        assert self.config.max_concurrency is not None
        delays = ramp_delays_from_floor(self.config.concurrency, self.config.max_concurrency, self.config.ramp_up)
        reporter = asyncio.create_task(self._progress_reporter())
        workers = [asyncio.create_task(self._worker(session, delay)) for delay in delays]
        try:
            # return_exceptions=True: an unexpected bug in one worker must
            # not leave the rest running as orphaned tasks against a
            # session the caller is about to close.
            results = await asyncio.gather(*workers, return_exceptions=True)
        finally:
            self._stop_event.set()
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
        return results

    async def _run_requests(self, session: "aiohttp.ClientSession") -> list:
        """requests mode's driver: grow concurrency while measured
        throughput stays under --target-rps, freeze once it's reached (or
        the concurrency ceiling is hit), then hold for
        REQUESTS_MODE_HOLD_SECONDS to confirm the rate actually sustains
        rather than being a one-second blip.

        Unlike break mode's pre-computed time-based ramp schedule, this
        grows concurrency by spawning additional workers on demand, driven
        by each closed bucket's *measured* req/s - the ramp reacts to
        reality instead of following a fixed schedule.
        """
        floor = self.config.concurrency
        ceiling = self.config.max_concurrency
        target = self.config.target_rps
        assert target is not None  # enforced by build_config_from_args
        assert ceiling is not None  # requests mode has no unbounded option, unlike takedown

        workers = [asyncio.create_task(self._worker(session, 0.0)) for _ in range(floor)]
        allowed = floor
        phase = "ramping"
        hold_deadline: Optional[float] = None
        hold_start_index = 0
        sustained = True
        last_measured = 0.0

        while not self._stop_event.is_set():
            await asyncio.sleep(1.0)
            if time.monotonic() >= self._deadline:
                self._stop_event.set()
                break

            measured = self._last_full_bucket_rps()
            last_measured = measured
            print(
                f"[requests:{phase}] t={time.monotonic() - self._start_time:5.1f}s  "
                f"concurrency={allowed}  measured={measured:.1f} req/s  target={target:.1f} req/s",
                file=sys.stderr,
            )

            if phase == "ramping":
                if measured >= target or allowed >= ceiling:
                    reached = measured >= target
                    phase = "holding"
                    hold_deadline = time.monotonic() + REQUESTS_MODE_HOLD_SECONDS
                    hold_start_index = len(self.stats.buckets)
                    self.requests_outcome = RequestsModeOutcome(
                        target_rps=target,
                        reached=reached,
                        concurrency_at_target=allowed,
                        measured_rps_at_target=measured,
                    )
                    if reached:
                        print(
                            f"[requests] target reached (~{measured:.1f} req/s at concurrency "
                            f"{allowed}) - holding for {REQUESTS_MODE_HOLD_SECONDS:.0f}s",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"[requests] concurrency ceiling ({ceiling}) reached without hitting "
                            f"{target:.1f} req/s (~{measured:.1f} req/s measured) - holding at the "
                            f"ceiling for {REQUESTS_MODE_HOLD_SECONDS:.0f}s to confirm this is a "
                            "real plateau",
                            file=sys.stderr,
                        )
                else:
                    new_allowed = next_concurrency(allowed, measured, target, ceiling)
                    if new_allowed > allowed:
                        for _ in range(new_allowed - allowed):
                            workers.append(asyncio.create_task(self._worker(session, 0.0)))
                        allowed = new_allowed
            else:  # holding
                hold_buckets = self.stats.buckets[hold_start_index:]
                bp = find_breaking_point(hold_buckets)
                if bp is not None:
                    sustained = False
                    print(
                        f"[requests] target rate NOT sustained during hold - {bp.reason} - stopping early",
                        file=sys.stderr,
                    )
                    self._stop_event.set()
                    break
                if time.monotonic() >= hold_deadline:
                    self._stop_event.set()
                    break

        if self.requests_outcome is None:
            # Outer safety cap (REQUESTS_MODE_RAMP_SAFETY) fired while still
            # ramping, before reaching either --target-rps or the
            # concurrency ceiling (e.g. growth was still climbing slowly
            # toward a very high ceiling).
            self.requests_outcome = RequestsModeOutcome(
                target_rps=target,
                reached=False,
                concurrency_at_target=allowed,
                measured_rps_at_target=last_measured,
            )
        else:
            self.requests_outcome.sustained = sustained if phase == "holding" else None

        self._stop_event.set()
        results = await asyncio.gather(*workers, return_exceptions=True)
        return results

    async def _run_takedown(self, session: "aiohttp.ClientSession") -> list:
        """takedown mode's driver: ramp like `break` until a breaking point
        is found, freeze concurrency at exactly that level (cancelling any
        not-yet-started ramp workers so it doesn't keep climbing past it),
        then hold for `--minutes`, escalating concurrency (`escalate_concurrency`)
        each time the target is observed to have recovered. Always stops
        automatically once the hold window elapses - the *time* bound is
        always in force, but the *concurrency* ceiling
        (`self.config.max_concurrency`) is optional: if the user didn't pass
        --max-concurrency, escalation during the hold phase is unbounded,
        limited only by this machine's own resources rather than a guessed
        number. The initial ramp-to-breaking-point search still needs *some*
        finite target to schedule delays against, so it falls back to
        `default_break_max_concurrency` in that case - a separate concept
        from the (possibly unbounded) hold-phase escalation ceiling.
        """
        ramp_ceiling = (
            self.config.max_concurrency
            if self.config.max_concurrency is not None
            else default_break_max_concurrency(self.config.concurrency)
        )
        delays = ramp_delays_from_floor(self.config.concurrency, ramp_ceiling, self.config.ramp_up)
        workers = [asyncio.create_task(self._worker(session, delay)) for delay in delays]

        ramp_deadline = self._start_time + TAKEDOWN_RAMP_SAFETY
        breaking_point: Optional[BreakingPoint] = None
        while breaking_point is None and time.monotonic() < ramp_deadline:
            await asyncio.sleep(1.0)
            breaking_point = find_breaking_point(self.stats.buckets)
            print(
                f"[takedown:ramping] t={time.monotonic() - self._start_time:5.1f}s  "
                f"requests={self.stats.total_requests}  active={self._active_workers}",
                file=sys.stderr,
            )

        if breaking_point is None:
            print(
                f"[takedown] target never broke within the {TAKEDOWN_RAMP_SAFETY:.0f}s ramp "
                f"safety window, even at concurrency {ramp_ceiling} - stopping without a hold phase",
                file=sys.stderr,
            )
            self.takedown_outcome = TakedownOutcome(
                hold_minutes=self.config.takedown_minutes,
                breaking_point_found=False,
            )
            self._stop_event.set()
            results = await asyncio.gather(*workers, return_exceptions=True)
            return results

        # Freeze concurrency at exactly what's active right now: cancel any
        # workers still waiting on their staggered ramp-up delay so the hold
        # phase starts from the level that broke the target, not wherever
        # the ramp schedule would otherwise have kept climbing to.
        break_elapsed = time.monotonic() - self._start_time
        held_workers = []
        for worker, delay in zip(workers, delays):
            if delay > break_elapsed:
                worker.cancel()
            else:
                held_workers.append(worker)
        workers = held_workers
        allowed = self._active_workers
        concurrency_at_break = allowed
        print(
            f"[takedown] breaking point reached at t={breaking_point.bucket_index}s "
            f"(active load ~{allowed}) - holding for {self.config.takedown_minutes:.1f} minute(s), "
            "escalating whenever the target recovers",
            file=sys.stderr,
        )

        ceiling = self.config.max_concurrency  # None = unbounded escalation
        hold_deadline = time.monotonic() + self.config.takedown_minutes * 60.0
        escalations = 0
        while time.monotonic() < hold_deadline and not self._stop_event.is_set():
            await asyncio.sleep(1.0)
            recent = self.stats.buckets[-5:]
            recovered = find_breaking_point(recent, sustained_buckets=2) is None
            at_ceiling = ceiling is not None and allowed >= ceiling
            if recovered and not at_ceiling:
                new_allowed = escalate_concurrency(allowed, ceiling)
                if new_allowed > allowed:
                    for _ in range(new_allowed - allowed):
                        workers.append(asyncio.create_task(self._worker(session, 0.0)))
                    escalations += 1
                    print(
                        f"[takedown] target recovered - escalating concurrency {allowed} -> {new_allowed}",
                        file=sys.stderr,
                    )
                    allowed = new_allowed
            status = "recovered" if recovered else "down"
            if recovered and at_ceiling:
                status += " (at concurrency ceiling - can't escalate further)"
            print(
                f"[takedown:holding] t={time.monotonic() - self._start_time:5.1f}s  "
                f"status={status}  concurrency={allowed}",
                file=sys.stderr,
            )

        self.takedown_outcome = TakedownOutcome(
            hold_minutes=self.config.takedown_minutes,
            breaking_point_found=True,
            concurrency_at_break=concurrency_at_break,
            escalations=escalations,
            final_concurrency=allowed,
        )
        self._stop_event.set()
        results = await asyncio.gather(*workers, return_exceptions=True)
        return results

    async def run(self) -> None:
        # 0 is aiohttp's own convention for "no connector-level cap" - used
        # when max_concurrency is None (takedown mode, no --max-concurrency
        # given): escalation should be limited by this machine's actual
        # resources, not throttled back down by a guessed connector limit.
        connector_limit = self.config.max_concurrency if self.config.max_concurrency is not None else 0
        connector = aiohttp.TCPConnector(limit=connector_limit, ssl=self.config.verify_tls)
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)

        self._start_time = time.monotonic()
        self._deadline = self._start_time + self.config.duration

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            heartbeat = asyncio.create_task(self._heartbeat())
            try:
                if self.config.mode == "break":
                    results = await self._run_break(session)
                elif self.config.mode == "requests":
                    results = await self._run_requests(session)
                else:
                    results = await self._run_takedown(session)
            finally:
                self._stop_event.set()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

        self.stats.duration = time.monotonic() - self._start_time

        for result in results:
            if isinstance(result, Exception):
                print(f"[warning] a worker crashed: {result!r}", file=sys.stderr)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _prompt(question: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer if answer else (default or "")


def _confirm_authorization(mode: str) -> None:
    print(
        "\nThis tool generates real concurrent load and can degrade or take down\n"
        "a target server. Only use it against systems you own or are explicitly\n"
        "authorized to test.\n"
    )
    if mode == "takedown":
        print(
            "takedown mode deliberately keeps the target down for the configured\n"
            "--minutes window, escalating load whenever it recovers. It stops\n"
            "automatically after that window (or immediately on Ctrl+C), but for\n"
            "that entire window the target will be unavailable to everyone, not\n"
            "just you.\n"
        )
    answer = input("Do you own or have authorization to test this target? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborting: authorization not confirmed.", file=sys.stderr)
        sys.exit(1)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Async HTTP load/stress tester. Three modes: 'break' ramps concurrency "
            "up until your target starts failing and reports the breaking point; "
            "'requests' ramps up until measured throughput reaches --target-rps, "
            "then holds to confirm it's sustained; 'takedown' ramps past the "
            "breaking point and holds the target down for a fixed number of "
            "minutes, escalating whenever it recovers, so you can test your own "
            "defenses in real time."
        )
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Skip the interactive authorization confirmation")
    # Ensures these attributes always exist on the parsed namespace even when
    # no subcommand is given (fully interactive invocation), since they're
    # otherwise only defined on the `break`/`requests`/`takedown` subparsers below.
    parser.set_defaults(
        url=None, concurrency=None, max_concurrency=None, timeout=10.0,
        method="GET", insecure=False, duration=None, rps=None, target_rps=None,
        minutes=None,
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", help="Target URL, domain, or IP")
    common.add_argument("-c", "--concurrency", type=int, help="Starting/ramp-floor number of concurrent connections (default: 10)")
    common.add_argument("--max-concurrency", type=int, default=None, help="Ceiling to ramp concurrency up to (default depends on mode - see README)")
    common.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds (default: 10)")
    common.add_argument("--method", default="GET", help="HTTP method to use (default: GET)")
    common.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    common.add_argument("-y", "--yes", action="store_true", help="Skip the interactive authorization confirmation")

    subparsers = parser.add_subparsers(dest="mode")

    break_parser = subparsers.add_parser(
        "break", parents=[common],
        help="Ramp concurrency up until the target starts failing, and report the breaking point.",
    )
    break_parser.add_argument(
        "-d", "--duration", type=float, default=None,
        help=f"Outer safety cap in seconds, in case the target never breaks (default: {DEFAULT_BREAK_DURATION:.0f})",
    )
    break_parser.add_argument("--rps", type=float, help="Optional global rate limit (requests/second) across all workers")

    requests_parser = subparsers.add_parser(
        "requests", parents=[common],
        help="Ramp concurrency up until measured throughput reaches --target-rps, then hold to confirm it's sustained.",
    )
    requests_parser.add_argument("--target-rps", type=float, default=None, help="Target requests/second to ramp toward and hold")

    takedown_parser = subparsers.add_parser(
        "takedown", parents=[common],
        help=(
            "Ramp concurrency past the breaking point, then hold the target down for "
            "a fixed number of minutes - escalating whenever it recovers - so you can "
            "test your own defenses (rate limiting, autoscaling, alerting) in real time."
        ),
    )
    takedown_parser.add_argument(
        "-m", "--minutes", type=float, default=None,
        help="Minutes to hold the target down for once it breaks (required)",
    )

    return parser.parse_args(argv)


def build_config_from_args(args: argparse.Namespace) -> RunConfig:
    url = args.url or _prompt("Target URL / domain / IP")
    url = normalize_url(url)

    mode = args.mode
    if mode is None:
        mode_answer = _prompt(
            "Mode - 'break' to find the breaking point, 'requests' to ramp to a "
            "target rate, 'takedown' to hold the target down for a fixed window",
            "break",
        )
        mode_answer = mode_answer.strip().lower()
        if mode_answer.startswith("r"):
            mode = "requests"
        elif mode_answer.startswith("t"):
            mode = "takedown"
        else:
            mode = "break"
    if mode not in ("break", "requests", "takedown"):
        raise ValueError(f"Unknown mode '{mode}' - expected 'break', 'requests', or 'takedown'")

    concurrency = (
        args.concurrency
        if args.concurrency is not None
        else int(_prompt("Concurrency (starting/ramp-floor parallel connections)", "10"))
    )
    if concurrency <= 0:
        raise ValueError("concurrency must be a positive integer")

    if args.timeout <= 0:
        raise ValueError("--timeout must be a positive number of seconds")

    if mode == "break":
        return _build_break_config(args, url, concurrency)
    if mode == "requests":
        return _build_requests_config(args, url, concurrency)
    return _build_takedown_config(args, url, concurrency)


def _build_break_config(args: argparse.Namespace, url: str, concurrency: int) -> RunConfig:
    max_concurrency = args.max_concurrency
    if max_concurrency is None:
        max_concurrency = default_break_max_concurrency(concurrency)
        print(
            f"[break] --max-concurrency defaulted to {max_concurrency} "
            f"({max_concurrency // concurrency}x -c) - override with --max-concurrency if you want a different ceiling",
            file=sys.stderr,
        )
    elif max_concurrency < concurrency:
        raise ValueError(
            f"--max-concurrency ({max_concurrency}) must be greater than or equal to "
            f"-c/--concurrency ({concurrency}) - it's the ceiling the ramp climbs toward."
        )

    duration = args.duration if args.duration is not None else DEFAULT_BREAK_DURATION
    if duration <= 0:
        raise ValueError("--duration must be a positive number of seconds")

    rps = args.rps
    if rps is not None and rps <= 0:
        raise ValueError("--rps must be a positive number")

    # Ramp across ~90% of the safety-cap duration, leaving a tail at full
    # concurrency so the top of the climb gets a chance to send something
    # before the run ends, rather than ramping right up to the deadline.
    ramp_up = duration * 0.9

    return RunConfig(
        mode="break",
        url=url,
        concurrency=concurrency,
        max_concurrency=max_concurrency,
        method=args.method,
        timeout=args.timeout,
        verify_tls=not args.insecure,
        duration=duration,
        ramp_up=ramp_up,
        rps=rps,
    )


def _build_requests_config(args: argparse.Namespace, url: str, concurrency: int) -> RunConfig:
    target_rps = args.target_rps
    if target_rps is None:
        target_rps = float(_prompt("Target requests/second to ramp toward and hold", "100"))
    if target_rps <= 0:
        raise ValueError("--target-rps must be a positive number")

    max_concurrency = args.max_concurrency
    if max_concurrency is None:
        max_concurrency = default_requests_max_concurrency(concurrency, target_rps)
        print(
            f"[requests] --max-concurrency defaulted to {max_concurrency} "
            f"(rough estimate: ~{ASSUMED_RPS_PER_WORKER:.0f} req/s per worker x3 headroom "
            f"for a {target_rps:.1f} req/s target) - override with --max-concurrency if this is off",
            file=sys.stderr,
        )
    elif max_concurrency < concurrency:
        raise ValueError(
            f"--max-concurrency ({max_concurrency}) must be greater than or equal to "
            f"-c/--concurrency ({concurrency}) - it's the ceiling the ramp climbs toward."
        )

    return RunConfig(
        mode="requests",
        url=url,
        concurrency=concurrency,
        max_concurrency=max_concurrency,
        method=args.method,
        timeout=args.timeout,
        verify_tls=not args.insecure,
        duration=REQUESTS_MODE_RAMP_SAFETY + REQUESTS_MODE_HOLD_SECONDS + 30.0,
        target_rps=target_rps,
    )


def _build_takedown_config(args: argparse.Namespace, url: str, concurrency: int) -> RunConfig:
    minutes = args.minutes
    if minutes is None:
        minutes = float(_prompt("Minutes to hold the target down for once it breaks", "5"))
    if minutes <= 0:
        raise ValueError("--minutes must be a positive number")

    max_concurrency = args.max_concurrency
    if max_concurrency is None:
        # Unlike break/requests, takedown has no default guess here: the
        # whole point of escalating against recovery is to go as high as
        # the target actually needs, not stop at an arbitrary number picked
        # in advance. The only cap left is this machine's own resources
        # (ephemeral ports, sockets, memory) - see README's "A note on
        # limits". Pass --max-concurrency explicitly to keep a hard ceiling.
        print(
            "[takedown] no --max-concurrency set - concurrency will escalate without a "
            "cap during the hold phase, limited only by this machine's own resources. "
            "Pass --max-concurrency to set an explicit ceiling instead.",
            file=sys.stderr,
        )
    elif max_concurrency < concurrency:
        raise ValueError(
            f"--max-concurrency ({max_concurrency}) must be greater than or equal to "
            f"-c/--concurrency ({concurrency}) - it's the ceiling the ramp climbs toward."
        )

    # Ramp across ~90% of the internal ramp-safety window, same spirit as
    # break mode's ramp_up default.
    ramp_up = TAKEDOWN_RAMP_SAFETY * 0.9

    return RunConfig(
        mode="takedown",
        url=url,
        concurrency=concurrency,
        max_concurrency=max_concurrency,
        method=args.method,
        timeout=args.timeout,
        verify_tls=not args.insecure,
        duration=TAKEDOWN_RAMP_SAFETY + minutes * 60.0 + 30.0,
        ramp_up=ramp_up,
        takedown_minutes=minutes,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config_from_args(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except EOFError:
        print("Error: no input available for an interactive prompt (are you running without a TTY?).", file=sys.stderr)
        return 1

    if not args.yes:
        try:
            _confirm_authorization(config.mode)
        except EOFError:
            print("Error: no input available to confirm authorization; pass --yes to skip this prompt.", file=sys.stderr)
            return 1

    generator = LoadGenerator(config)
    interrupted = False
    try:
        asyncio.run(generator.run())
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted - shutting down, computing partial report...", file=sys.stderr)
        # generator.stats was populated incrementally by workers, so it still
        # holds whatever was completed before the interrupt; duration wasn't
        # set by run() in this path, so fill it in from the elapsed time.
        if generator.start_time:
            generator.stats.duration = time.monotonic() - generator.start_time

    breaking_point = find_breaking_point(generator.stats.buckets)
    print()
    print(build_report(generator.stats, breaking_point, generator.requests_outcome, generator.takedown_outcome))
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
