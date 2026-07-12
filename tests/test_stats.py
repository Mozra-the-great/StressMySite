"""Unit tests for the pure, network-free helpers in stress_my_site.py.

These cover the math and control-flow logic (URL normalization, percentiles,
ramp-up scheduling, breaking-point detection, report formatting) without
touching a real network or event loop.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stress_my_site import (  # noqa: E402
    Bucket,
    RunStats,
    TokenBucketLimiter,
    build_config_from_args,
    build_report,
    find_breaking_point,
    normalize_url,
    parse_args,
    percentile,
    ramp_delays,
)


class TestNormalizeUrl:
    def test_bare_domain_gets_https(self):
        assert normalize_url("example.com") == "https://example.com"

    def test_bare_ip_gets_https(self):
        assert normalize_url("10.0.0.5") == "https://10.0.0.5"

    def test_existing_scheme_preserved(self):
        assert normalize_url("http://10.0.0.5") == "http://10.0.0.5"
        assert normalize_url("https://example.com") == "https://example.com"

    def test_whitespace_trimmed(self):
        assert normalize_url("  example.com  ") == "https://example.com"

    def test_host_and_port_gets_https(self):
        # urlparse() misreads "localhost" as a scheme for "localhost:8080";
        # this is the regression case for that bug.
        assert normalize_url("localhost:8080") == "https://localhost:8080"
        assert normalize_url("my-server:9000") == "https://my-server:9000"

    def test_existing_scheme_with_port_preserved(self):
        assert normalize_url("http://localhost:8080") == "http://localhost:8080"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            normalize_url("   ")


class TestPercentile:
    def test_single_element(self):
        assert percentile([5.0], 50) == 5.0
        assert percentile([5.0], 0) == 5.0
        assert percentile([5.0], 100) == 5.0

    def test_p0_is_min_p100_is_max(self):
        values = sorted([1.0, 2.0, 3.0, 4.0, 5.0])
        assert percentile(values, 0) == 1.0
        assert percentile(values, 100) == 5.0

    def test_p50_median_odd_count(self):
        values = sorted([1.0, 2.0, 3.0])
        assert percentile(values, 50) == 2.0

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            percentile([], 50)

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            percentile([1.0, 2.0], 101)
        with pytest.raises(ValueError):
            percentile([1.0, 2.0], -1)


class TestRampDelays:
    def test_no_rampup_all_zero(self):
        assert ramp_delays(5, 0) == [0.0] * 5
        assert ramp_delays(5, -1) == [0.0] * 5

    def test_single_worker_no_rampup(self):
        assert ramp_delays(1, 10) == [0.0]

    def test_linear_staggering(self):
        delays = ramp_delays(5, 20)
        assert delays[0] == 0.0
        assert delays[-1] == 20.0
        assert delays == sorted(delays)

    def test_zero_concurrency_raises(self):
        with pytest.raises(ValueError):
            ramp_delays(0, 10)


class TestFindBreakingPoint:
    def test_no_buckets_returns_none(self):
        assert find_breaking_point([]) is None

    def test_healthy_run_returns_none(self):
        buckets = []
        for i in range(5):
            b = Bucket(index=i, requests=100, successes=100, errors=0, active_load=10)
            b.latencies = [0.05] * 100
            buckets.append(b)
        assert find_breaking_point(buckets) is None

    def test_sustained_error_spike_detected(self):
        buckets = []
        # 3 healthy buckets
        for i in range(3):
            b = Bucket(index=i, requests=100, successes=100, errors=0, active_load=10 * (i + 1))
            b.latencies = [0.05] * 100
            buckets.append(b)
        # 2 consecutive buckets with a high rate of 5xx (sustained, hard failures)
        for i in range(3, 5):
            b = Bucket(
                index=i,
                requests=100,
                successes=20,
                errors=80,
                server_errors=80,
                hard_failures=80,
                active_load=10 * (i + 1),
            )
            b.latencies = [0.05] * 20
            buckets.append(b)

        result = find_breaking_point(buckets, err_threshold=0.05, sustained_buckets=2)
        assert result is not None
        assert result.bucket_index == 3
        assert result.active_load == 40

    def test_single_transient_error_not_flagged(self):
        buckets = []
        for i in range(5):
            is_blip = i == 2  # one-off blip, not sustained
            errors = 80 if is_blip else 0
            successes = 20 if is_blip else 100
            b = Bucket(
                index=i,
                requests=100,
                successes=successes,
                errors=errors,
                server_errors=errors,
                hard_failures=errors,
                active_load=10,
            )
            b.latencies = [0.05] * 100
            buckets.append(b)
        assert find_breaking_point(buckets, err_threshold=0.05, sustained_buckets=2) is None

    def test_4xx_responses_do_not_trigger_breaking_point(self):
        # 4xx counts toward `errors` (overall failure stats) but must NOT
        # count toward the hard-failure rate used for breaking-point
        # detection, since a target consistently returning e.g. 404 is
        # behaving as designed, not failing under load.
        buckets = []
        for i in range(5):
            b = Bucket(index=i, requests=100, successes=20, errors=80, active_load=10)
            b.latencies = [0.05] * 100
            buckets.append(b)
        assert find_breaking_point(buckets, err_threshold=0.05, sustained_buckets=2) is None

    def test_empty_buckets_are_ignored(self):
        buckets = [Bucket(index=0, requests=0)]
        assert find_breaking_point(buckets) is None


class TestTokenBucketLimiter:
    def test_default_capacity_equals_rps(self):
        limiter = TokenBucketLimiter(rps=50)
        assert limiter.capacity == 50
        assert limiter._tokens == 50

    def test_explicit_capacity_used(self):
        limiter = TokenBucketLimiter(rps=50, capacity=5)
        assert limiter.capacity == 5
        assert limiter._tokens == 5

    def test_acquire_consumes_one_token_without_blocking_when_available(self):
        async def scenario() -> float:
            limiter = TokenBucketLimiter(rps=10, capacity=5)
            await limiter.acquire()
            return limiter._tokens

        remaining = asyncio.run(scenario())
        assert remaining == pytest.approx(4, abs=0.05)

    def test_tokens_never_exceed_capacity(self):
        # Simulate a long idle period (e.g. the target was slow to respond)
        # by seeding `_last` far in the past, then confirm a refill still
        # clamps to `capacity` instead of letting a burst accumulate.
        limiter = TokenBucketLimiter(rps=1000, capacity=3)
        limiter._tokens = 0
        limiter._last -= 100  # pretend 100 idle seconds passed

        async def scenario() -> float:
            await limiter.acquire()
            return limiter._tokens

        remaining = asyncio.run(scenario())
        # after one acquire, tokens should be capacity - 1 at most, never
        # anywhere near the ~100,000 tokens 100s at rps=1000 would imply
        assert remaining <= limiter.capacity


class TestBuildConfigValidation:
    def test_ramp_up_with_requests_raises(self):
        args = parse_args(["--url", "https://example.com", "-c", "5", "-n", "100", "--ramp-up", "10", "-y"])
        with pytest.raises(ValueError, match="only supported together with --duration"):
            build_config_from_args(args)

    def test_ramp_up_with_duration_is_allowed(self):
        args = parse_args(["--url", "https://example.com", "-c", "5", "-d", "10", "--ramp-up", "5", "-y"])
        config = build_config_from_args(args)
        assert config.ramp_up == 5

    def test_zero_concurrency_raises(self):
        args = parse_args(["--url", "https://example.com", "-c", "0", "-n", "100", "-y"])
        with pytest.raises(ValueError, match="positive integer"):
            build_config_from_args(args)

    def test_negative_rps_raises(self):
        args = parse_args(["--url", "https://example.com", "-c", "5", "-n", "100", "--rps", "-1", "-y"])
        with pytest.raises(ValueError, match="--rps must be a positive number"):
            build_config_from_args(args)

    def test_both_modes_raises(self):
        args = parse_args(["--url", "https://example.com", "-c", "5", "-n", "100", "-d", "10", "-y"])
        with pytest.raises(ValueError, match="not both"):
            build_config_from_args(args)

    def test_zero_timeout_raises(self):
        args = parse_args(["--url", "https://example.com", "-c", "5", "-n", "100", "--timeout", "0", "-y"])
        with pytest.raises(ValueError, match="--timeout must be a positive"):
            build_config_from_args(args)

    def test_host_port_url_normalized_end_to_end(self):
        args = parse_args(["--url", "localhost:8080", "-c", "5", "-n", "100", "-y"])
        config = build_config_from_args(args)
        assert config.url == "https://localhost:8080"


class TestBuildReport:
    def test_report_contains_key_sections(self):
        stats = RunStats(url="https://example.com")
        stats.total_requests = 10
        stats.successes = 9
        stats.errors = 1
        stats.status_counts = {200: 9, 500: 1}
        stats.latencies = [0.01, 0.02, 0.03, 0.04, 0.05]
        stats.duration = 2.0

        report = build_report(stats, None)
        assert "example.com" in report
        assert "Total requests:    10" in report
        assert "200:" in report
        assert "p95:" in report
        assert "No breaking point detected" in report

    def test_report_shows_breaking_point(self):
        from stress_my_site import BreakingPoint

        stats = RunStats(url="https://example.com")
        stats.total_requests = 10
        stats.successes = 5
        stats.errors = 5
        stats.duration = 5.0
        bp = BreakingPoint(bucket_index=3, active_load=42, failure_rate=0.5, p95_latency=1.2, reason="failure rate 50% exceeded 5% threshold")

        report = build_report(stats, bp)
        assert "BREAKING POINT DETECTED" in report
        assert "42" in report
