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

Usage examples:
    python stress_my_site.py --url https://example.com -c 50 -n 5000
    python stress_my_site.py --url http://localhost:8080 -c 100 -d 30 --ramp-up 20
    # Keep climbing from 100 up to 20,000 concurrent connections across the
    # whole 5-minute run, stopping as soon as a breaking point is detected:
    python stress_my_site.py --url http://localhost:8080 -c 100 -d 300 \
        --max-concurrency 20000 --stop-on-break
    python stress_my_site.py            # interactive prompts for everything

IMPORTANT: Only point this at systems you own or are explicitly authorized to
test. Sustained concurrent load can degrade or take down a service.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

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
        longer than `max(baseline_p95 * stall_factor, 1.0)` seconds. That
        floor matters — a target with a 3s baseline latency can legitimately
        produce a couple of empty one-second buckets between completions
        while still being healthy, so a bare "any empty second" rule would
        false-positive on slow-but-fine targets. Gauging the gap against the
        baseline latency instead only flags a *real* stall: workers in
        flight noticeably longer than they normally take.

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
            if gap_seconds > stall_threshold:
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


def build_report(stats: RunStats, breaking_point: Optional[BreakingPoint]) -> str:
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
    url: str
    concurrency: int
    method: str = "GET"
    total_requests: Optional[int] = None  # count mode
    duration: Optional[float] = None  # duration mode
    ramp_up: float = 0.0
    max_concurrency: Optional[int] = None  # if set, ramp climbs to this instead of plateauing at `concurrency`
    stop_on_break: bool = False  # end the run early once a breaking point is detected
    timeout: float = 10.0
    verify_tls: bool = True
    rps: Optional[float] = None  # optional global rate limit


class LoadGenerator:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.stats = RunStats(url=config.url)
        self._remaining = config.total_requests  # None in duration mode
        self._deadline: Optional[float] = None
        self._start_time = 0.0
        self._stop_event = asyncio.Event()
        self._rate_limiter = TokenBucketLimiter(config.rps) if config.rps else None
        self._active_workers = 0

    @property
    def start_time(self) -> float:
        return self._start_time

    def _bucket_for(self, elapsed: float) -> Bucket:
        index = int(elapsed)
        while len(self.stats.buckets) <= index:
            self.stats.buckets.append(Bucket(index=len(self.stats.buckets)))
        return self.stats.buckets[index]

    async def _claim_request(self) -> bool:
        """Return True if this worker may send one more request."""
        if self._stop_event.is_set():
            return False
        if self.config.duration is not None:
            return time.monotonic() < self._deadline
        # count mode
        if self._remaining is None or self._remaining <= 0:
            return False
        self._remaining -= 1
        return True

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
            if self.config.stop_on_break:
                bp = find_breaking_point(self.stats.buckets)
                if bp is not None:
                    print(
                        f"[stop-on-break] breaking point detected at t={bp.bucket_index}s "
                        f"(active load ~{bp.active_load}) - stopping early",
                        file=sys.stderr,
                    )
                    self._stop_event.set()
                    return

    async def run(self) -> None:
        # When --max-concurrency is set, the ramp starts at `concurrency`
        # (the floor - the historical meaning of `-c`) and climbs toward
        # `max_concurrency` instead of plateauing at `concurrency` forever.
        # The connector limit must track whichever value workers can
        # actually reach, or connections throttle back down to the old
        # ceiling regardless of how many workers exist.
        if self.config.max_concurrency:
            peak_concurrency = self.config.max_concurrency
            delays = ramp_delays_from_floor(self.config.concurrency, peak_concurrency, self.config.ramp_up)
        else:
            peak_concurrency = self.config.concurrency
            delays = ramp_delays(peak_concurrency, self.config.ramp_up)
        connector = aiohttp.TCPConnector(limit=peak_concurrency, ssl=self.config.verify_tls)
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)

        self._start_time = time.monotonic()
        if self.config.duration is not None:
            self._deadline = self._start_time + self.config.duration

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            reporter = asyncio.create_task(self._progress_reporter())
            heartbeat = asyncio.create_task(self._heartbeat())
            workers = [
                asyncio.create_task(self._worker(session, delay)) for delay in delays
            ]
            try:
                # return_exceptions=True: an unexpected bug in one worker
                # must not leave the rest running as orphaned tasks against a
                # session this `async with` block is about to close.
                results = await asyncio.gather(*workers, return_exceptions=True)
            finally:
                self._stop_event.set()
                reporter.cancel()
                heartbeat.cancel()
                await asyncio.gather(reporter, heartbeat, return_exceptions=True)

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


def _confirm_authorization() -> None:
    print(
        "\nThis tool generates real concurrent load and can degrade or take down\n"
        "a target server. Only use it against systems you own or are explicitly\n"
        "authorized to test.\n"
    )
    answer = input("Do you own or have authorization to test this target? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborting: authorization not confirmed.", file=sys.stderr)
        sys.exit(1)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Async HTTP load/stress tester - finds the load level where your server breaks."
    )
    parser.add_argument("--url", help="Target URL, domain, or IP")
    parser.add_argument("-c", "--concurrency", type=int, help="Number of concurrent connections")
    parser.add_argument("-n", "--requests", type=int, help="Total number of requests to send")
    parser.add_argument("-d", "--duration", type=float, help="Duration in seconds to sustain load")
    parser.add_argument("--ramp-up", type=float, default=None, help="Seconds to linearly ramp concurrency up (default: 0, i.e. full concurrency instantly; defaults to ~90%% of --duration if --max-concurrency is given)")
    parser.add_argument("--max-concurrency", type=int, default=None, help="Ceiling to continuously ramp concurrency up to over the run, instead of plateauing at -c/--concurrency once the ramp finishes (requires --duration; use this to push toward tens of thousands of req/s)")
    parser.add_argument("--stop-on-break", action="store_true", help="End the run as soon as a breaking point is detected, instead of running for the full --duration/--requests regardless")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds (default: 10)")
    parser.add_argument("--method", default="GET", help="HTTP method to use (default: GET)")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--rps", type=float, help="Optional global rate limit (requests/second) across all workers")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip the interactive authorization confirmation")
    return parser.parse_args(argv)


def build_config_from_args(args: argparse.Namespace) -> RunConfig:
    url = args.url or _prompt("Target URL / domain / IP")
    url = normalize_url(url)

    concurrency = (
        args.concurrency
        if args.concurrency is not None
        else int(_prompt("Concurrency (parallel connections)", "10"))
    )
    if concurrency <= 0:
        raise ValueError("concurrency must be a positive integer")

    total_requests = args.requests
    duration = args.duration
    if total_requests is None and duration is None:
        mode = _prompt("Load mode - 'requests' for a fixed total, 'duration' for a time-based run", "requests")
        if mode.lower().startswith("d"):
            duration = float(_prompt("Duration in seconds", "30"))
        else:
            total_requests = int(_prompt("Total number of requests", "1000"))
    if total_requests is not None and duration is not None:
        raise ValueError("Specify either --requests or --duration, not both")
    if total_requests is not None and total_requests <= 0:
        raise ValueError("--requests must be a positive integer")
    if duration is not None and duration <= 0:
        raise ValueError("--duration must be a positive number of seconds")

    max_concurrency = args.max_concurrency
    if max_concurrency is not None:
        if total_requests is not None:
            raise ValueError(
                "--max-concurrency is only supported together with --duration, not "
                "--requests: it needs a time window to ramp across. Use --duration instead."
            )
        if max_concurrency <= concurrency:
            raise ValueError(
                f"--max-concurrency ({max_concurrency}) must be greater than "
                f"-c/--concurrency ({concurrency}) - it's the ceiling the ramp climbs "
                "toward, not the starting point."
            )
        if args.ramp_up is not None and args.ramp_up <= 0:
            raise ValueError(
                "--max-concurrency needs a positive --ramp-up to climb across "
                "(or omit --ramp-up to use the ~90%-of-duration default) - "
                "otherwise every --max-concurrency worker starts at once instead "
                "of ramping in."
            )

    ramp_up_arg = args.ramp_up
    if ramp_up_arg is None:
        # No explicit --ramp-up: default to ramping across (almost) the whole
        # run when --max-concurrency is set (that's the point of a continuous
        # climb) - leave a 10% tail at full concurrency rather than ramping
        # right up to the deadline, otherwise the top of the climb barely
        # gets a chance to send anything. Otherwise keep the historical
        # default of no ramp at all.
        ramp_up = duration * 0.9 if (max_concurrency is not None and duration is not None) else 0.0
    else:
        ramp_up = ramp_up_arg
    if ramp_up < 0:
        raise ValueError("--ramp-up must not be negative")
    if ramp_up > 0 and total_requests is not None:
        raise ValueError(
            "--ramp-up is only supported together with --duration, not --requests: "
            "in count mode, workers started immediately drain the shared request "
            "budget before staggered workers get a chance to start, so the ramp "
            "never actually happens. Use --duration instead."
        )
    if ramp_up_arg is not None and ramp_up > 0 and duration is not None and ramp_up >= duration:
        raise ValueError(
            f"--ramp-up ({ramp_up}s) must be less than --duration ({duration}s) - "
            "otherwise workers are still staggering in when the run ends and the "
            "ramp never reaches full concurrency."
        )

    if args.timeout <= 0:
        raise ValueError("--timeout must be a positive number of seconds")
    if args.rps is not None and args.rps <= 0:
        raise ValueError("--rps must be a positive number")

    return RunConfig(
        url=url,
        concurrency=concurrency,
        method=args.method,
        total_requests=total_requests,
        duration=duration,
        ramp_up=ramp_up,
        max_concurrency=max_concurrency,
        stop_on_break=args.stop_on_break,
        timeout=args.timeout,
        verify_tls=not args.insecure,
        rps=args.rps,
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
            _confirm_authorization()
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
    print(build_report(generator.stats, breaking_point))
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
