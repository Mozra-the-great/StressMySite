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
    DEFAULT_BREAK_DURATION,
    REQUESTS_MODE_HOLD_SECONDS,
    REQUESTS_MODE_RAMP_SAFETY,
    TAKEDOWN_RAMP_SAFETY,
    Bucket,
    RunStats,
    TokenBucketLimiter,
    build_config_from_args,
    build_report,
    default_break_max_concurrency,
    default_requests_max_concurrency,
    escalate_concurrency,
    find_breaking_point,
    next_concurrency,
    normalize_url,
    parse_args,
    percentile,
    ramp_delays,
    ramp_delays_from_floor,
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


class TestRampDelaysFromFloor:
    def test_floor_workers_start_immediately(self):
        delays = ramp_delays_from_floor(5, 20, 30)
        assert delays[:5] == [0.0] * 5

    def test_extra_workers_stagger_up_to_ramp_seconds(self):
        delays = ramp_delays_from_floor(5, 20, 30)
        extra = delays[5:]
        assert len(extra) == 15
        assert extra == sorted(extra)
        assert extra[-1] == 30.0
        assert extra[0] > 0.0

    def test_no_extra_workers_all_start_immediately(self):
        assert ramp_delays_from_floor(10, 10, 30) == [0.0] * 10

    def test_zero_ramp_seconds_all_start_immediately(self):
        assert ramp_delays_from_floor(5, 20, 0) == [0.0] * 20

    def test_peak_below_floor_raises(self):
        with pytest.raises(ValueError):
            ramp_delays_from_floor(20, 5, 30)

    def test_zero_floor_raises(self):
        with pytest.raises(ValueError):
            ramp_delays_from_floor(0, 20, 30)


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

    def test_total_stall_detected_at_low_concurrency(self):
        # Regression test for the original bug report: with a small worker
        # count (e.g. ~10), a real slowdown can stall *every* worker at
        # once, producing whole seconds with zero completions. Those used
        # to be dropped from `data_buckets` entirely, so a full stall could
        # never accumulate `sustained_buckets` in a row and was invisible.
        buckets = []
        for i in range(3):  # healthy baseline, fast responses
            b = Bucket(index=i, requests=100, successes=100, active_load=10)
            b.latencies = [0.02] * 100
            buckets.append(b)
        for i in range(3, 6):  # every worker wedged - nothing completes
            buckets.append(Bucket(index=i, requests=0, active_load=10))

        result = find_breaking_point(buckets, sustained_buckets=2)
        assert result is not None
        assert "stalled" in result.reason

    def test_slow_but_healthy_target_not_flagged_as_stalled(self):
        # A target with several-second latency legitimately produces empty
        # one-second buckets between completions while working fine. The
        # stall check must be judged against the baseline latency, not
        # "any empty second", or this would false-positive.
        buckets = []
        # baseline established at ~3.5s latency
        b0 = Bucket(index=0, requests=1, successes=1, active_load=1)
        b0.latencies = [3.5]
        buckets.append(b0)
        for i in range(1, 3):  # gap while the next request is still in flight
            buckets.append(Bucket(index=i, requests=0, active_load=1))
        b3 = Bucket(index=3, requests=1, successes=1, active_load=1)
        b3.latencies = [3.5]
        buckets.append(b3)
        for i in range(4, 6):
            buckets.append(Bucket(index=i, requests=0, active_load=1))

        assert find_breaking_point(buckets, sustained_buckets=2) is None

    def test_no_baseline_yet_does_not_flag_stall(self):
        # Cold start: workers active, nothing has completed yet - a slow-
        # but-healthy target mid-first-request (e.g. multi-second latency)
        # is indistinguishable from a stall until a baseline p95 exists, so
        # this must NOT fire. Without a `baseline_p95 > 0` guard, the stall
        # threshold collapses to its 1.0s floor and fires within 2-3s
        # against any target slower than that - a real regression this
        # test would have caught (see stress_my_site.py history).
        buckets = [Bucket(index=i, requests=0, active_load=10) for i in range(3)]
        assert find_breaking_point(buckets, sustained_buckets=2) is None

    def test_stall_after_first_empty_second_not_yet_flagged(self):
        # A single empty second alone (gap=1) must not trigger even at a
        # near-zero baseline latency - the 1-second bucket granularity means
        # gap=1 is the noise floor, not yet a signal.
        buckets = [Bucket(index=0, requests=100, successes=100, active_load=10)]
        buckets[0].latencies = [0.01] * 100
        buckets.append(Bucket(index=1, requests=0, active_load=10))
        assert find_breaking_point(buckets, sustained_buckets=2) is None


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


class TestNextConcurrency:
    def test_frozen_when_already_at_target(self):
        # caller is expected to stop calling once measured >= target, but
        # the function itself should still make forward progress if asked
        assert next_concurrency(10, measured_rps=100, target_rps=100, ceiling=1000) > 10

    def test_no_signal_yet_doubles(self):
        assert next_concurrency(10, measured_rps=0, target_rps=500, ceiling=1000) == 20

    def test_proportional_growth_under_target(self):
        # measured 50 at concurrency 10, target 100 -> roughly double
        assert next_concurrency(10, measured_rps=50, target_rps=100, ceiling=1000) == 20

    def test_growth_capped_at_2x_per_step(self):
        # measured 10 at concurrency 10, target 1000 -> ratio is huge but
        # capped at 2x so a single noisy sample can't cause a huge overshoot
        assert next_concurrency(10, measured_rps=10, target_rps=1000, ceiling=10_000) == 20

    def test_always_advances_by_at_least_one(self):
        # measured already very close to target - ratio near 1 must not
        # round down to zero growth
        assert next_concurrency(10, measured_rps=99, target_rps=100, ceiling=1000) == 11

    def test_clamped_at_ceiling(self):
        assert next_concurrency(950, measured_rps=10, target_rps=1000, ceiling=1000) == 1000

    def test_non_positive_current_raises(self):
        with pytest.raises(ValueError):
            next_concurrency(0, measured_rps=10, target_rps=100, ceiling=1000)

    def test_ceiling_below_current_raises(self):
        with pytest.raises(ValueError):
            next_concurrency(100, measured_rps=10, target_rps=1000, ceiling=50)


class TestDefaultMaxConcurrency:
    def test_break_default_is_200x_floor(self):
        assert default_break_max_concurrency(10) == 2000
        assert default_break_max_concurrency(50) == 10_000

    def test_requests_default_scales_with_target(self):
        low = default_requests_max_concurrency(10, target_rps=50)
        high = default_requests_max_concurrency(10, target_rps=5000)
        assert high > low

    def test_requests_default_never_below_floor(self):
        assert default_requests_max_concurrency(500, target_rps=1) == 500


class TestEscalateConcurrency:
    def test_grows_by_default_factor(self):
        assert escalate_concurrency(100, ceiling=1000) == 125  # +25%

    def test_always_advances_by_at_least_one(self):
        # small current where the factor alone would round down to no growth
        assert escalate_concurrency(2, ceiling=1000) == 3

    def test_custom_factor_respected(self):
        assert escalate_concurrency(100, ceiling=1000, factor=2.0) == 200

    def test_clamped_at_ceiling(self):
        assert escalate_concurrency(900, ceiling=1000, factor=2.0) == 1000

    def test_non_positive_current_raises(self):
        with pytest.raises(ValueError):
            escalate_concurrency(0, ceiling=1000)

    def test_ceiling_below_current_raises(self):
        with pytest.raises(ValueError):
            escalate_concurrency(100, ceiling=50)


class TestBuildConfigValidation:
    def test_zero_concurrency_raises(self):
        args = parse_args(["break", "--url", "https://example.com", "-c", "0", "-y"])
        with pytest.raises(ValueError, match="positive integer"):
            build_config_from_args(args)

    def test_zero_timeout_raises(self):
        args = parse_args(["break", "--url", "https://example.com", "-c", "5", "--timeout", "0", "-y"])
        with pytest.raises(ValueError, match="--timeout must be a positive"):
            build_config_from_args(args)

    def test_host_port_url_normalized_end_to_end(self):
        args = parse_args(["break", "--url", "localhost:8080", "-c", "5", "-y"])
        config = build_config_from_args(args)
        assert config.url == "https://localhost:8080"

    def test_unknown_mode_raises(self):
        with pytest.raises(SystemExit):
            parse_args(["bogus-mode", "--url", "https://example.com", "-y"])

    class TestBreakMode:
        def test_max_concurrency_defaults_to_200x_concurrency(self):
            args = parse_args(["break", "--url", "https://example.com", "-c", "10", "-y"])
            config = build_config_from_args(args)
            assert config.mode == "break"
            assert config.max_concurrency == 2000

        def test_explicit_max_concurrency_used(self):
            args = parse_args(["break", "--url", "https://example.com", "-c", "10", "--max-concurrency", "500", "-y"])
            config = build_config_from_args(args)
            assert config.max_concurrency == 500

        def test_max_concurrency_below_concurrency_raises(self):
            args = parse_args(["break", "--url", "https://example.com", "-c", "500", "--max-concurrency", "100", "-y"])
            with pytest.raises(ValueError, match="must be greater than or equal to"):
                build_config_from_args(args)

        def test_max_concurrency_equal_to_concurrency_is_allowed(self):
            # a flat, no-ramp run at a fixed concurrency is a legitimate
            # way to use break mode - just watch for a breaking point at a
            # single load level instead of climbing
            args = parse_args(["break", "--url", "https://example.com", "-c", "500", "--max-concurrency", "500", "-y"])
            config = build_config_from_args(args)
            assert config.max_concurrency == 500

        def test_duration_defaults_to_module_constant(self):
            args = parse_args(["break", "--url", "https://example.com", "-c", "10", "-y"])
            config = build_config_from_args(args)
            assert config.duration == DEFAULT_BREAK_DURATION

        def test_explicit_duration_respected(self):
            args = parse_args(["break", "--url", "https://example.com", "-c", "10", "-d", "60", "-y"])
            config = build_config_from_args(args)
            assert config.duration == 60.0

        def test_zero_duration_raises(self):
            args = parse_args(["break", "--url", "https://example.com", "-c", "10", "-d", "0", "-y"])
            with pytest.raises(ValueError, match="--duration must be a positive"):
                build_config_from_args(args)

        def test_ramp_up_is_90_percent_of_duration(self):
            args = parse_args(["break", "--url", "https://example.com", "-c", "10", "-d", "100", "-y"])
            config = build_config_from_args(args)
            assert config.ramp_up == pytest.approx(90.0)

        def test_negative_rps_raises(self):
            args = parse_args(["break", "--url", "https://example.com", "-c", "5", "--rps", "-1", "-y"])
            with pytest.raises(ValueError, match="--rps must be a positive number"):
                build_config_from_args(args)

        def test_rps_is_optional_and_stored(self):
            args = parse_args(["break", "--url", "https://example.com", "-c", "5", "--rps", "200", "-y"])
            config = build_config_from_args(args)
            assert config.rps == 200

    class TestRequestsMode:
        def test_target_rps_required_non_interactively(self):
            args = parse_args(["requests", "--url", "https://example.com", "-c", "10", "-y"])
            # no --target-rps given, so it falls back to an interactive
            # prompt; under pytest's captured stdin that raises OSError
            # (a real non-interactive shell would see EOFError instead -
            # main() handles both the same way)
            with pytest.raises((EOFError, OSError)):
                build_config_from_args(args)

        def test_target_rps_stored(self):
            args = parse_args(["requests", "--url", "https://example.com", "-c", "10", "--target-rps", "500", "-y"])
            config = build_config_from_args(args)
            assert config.mode == "requests"
            assert config.target_rps == 500

        def test_zero_target_rps_raises(self):
            args = parse_args(["requests", "--url", "https://example.com", "-c", "10", "--target-rps", "0", "-y"])
            with pytest.raises(ValueError, match="--target-rps must be a positive"):
                build_config_from_args(args)

        def test_max_concurrency_defaults_from_target_rps(self):
            args = parse_args(["requests", "--url", "https://example.com", "-c", "10", "--target-rps", "500", "-y"])
            config = build_config_from_args(args)
            assert config.max_concurrency == default_requests_max_concurrency(10, 500)

        def test_explicit_max_concurrency_used(self):
            args = parse_args([
                "requests", "--url", "https://example.com", "-c", "10",
                "--target-rps", "500", "--max-concurrency", "50", "-y",
            ])
            config = build_config_from_args(args)
            assert config.max_concurrency == 50

        def test_max_concurrency_below_concurrency_raises(self):
            args = parse_args([
                "requests", "--url", "https://example.com", "-c", "100",
                "--target-rps", "500", "--max-concurrency", "10", "-y",
            ])
            with pytest.raises(ValueError, match="must be greater than or equal to"):
                build_config_from_args(args)

        def test_duration_is_internal_safety_cap_not_exposed(self):
            args = parse_args(["requests", "--url", "https://example.com", "-c", "10", "--target-rps", "500", "-y"])
            config = build_config_from_args(args)
            assert config.duration == pytest.approx(REQUESTS_MODE_RAMP_SAFETY + REQUESTS_MODE_HOLD_SECONDS + 30.0)

    class TestTakedownMode:
        def test_minutes_required_non_interactively(self):
            args = parse_args(["takedown", "--url", "https://example.com", "-c", "10", "-y"])
            # no --minutes given and no stdin to prompt from in this test -
            # see the analogous requests-mode test for why both exception
            # types are accepted here
            with pytest.raises((EOFError, OSError)):
                build_config_from_args(args)

        def test_minutes_stored(self):
            args = parse_args(["takedown", "--url", "https://example.com", "-c", "10", "-m", "5", "-y"])
            config = build_config_from_args(args)
            assert config.mode == "takedown"
            assert config.takedown_minutes == 5

        def test_zero_minutes_raises(self):
            args = parse_args(["takedown", "--url", "https://example.com", "-c", "10", "-m", "0", "-y"])
            with pytest.raises(ValueError, match="--minutes must be a positive"):
                build_config_from_args(args)

        def test_negative_minutes_raises(self):
            args = parse_args(["takedown", "--url", "https://example.com", "-c", "10", "-m", "-1", "-y"])
            with pytest.raises(ValueError, match="--minutes must be a positive"):
                build_config_from_args(args)

        def test_max_concurrency_defaults_to_200x_concurrency(self):
            args = parse_args(["takedown", "--url", "https://example.com", "-c", "10", "-m", "5", "-y"])
            config = build_config_from_args(args)
            assert config.max_concurrency == 2000

        def test_explicit_max_concurrency_used(self):
            args = parse_args(["takedown", "--url", "https://example.com", "-c", "10", "-m", "5", "--max-concurrency", "500", "-y"])
            config = build_config_from_args(args)
            assert config.max_concurrency == 500

        def test_max_concurrency_below_concurrency_raises(self):
            args = parse_args(["takedown", "--url", "https://example.com", "-c", "500", "-m", "5", "--max-concurrency", "100", "-y"])
            with pytest.raises(ValueError, match="must be greater than or equal to"):
                build_config_from_args(args)

        def test_duration_is_internal_safety_cap_derived_from_minutes(self):
            args = parse_args(["takedown", "--url", "https://example.com", "-c", "10", "-m", "5", "-y"])
            config = build_config_from_args(args)
            assert config.duration == pytest.approx(TAKEDOWN_RAMP_SAFETY + 5 * 60.0 + 30.0)

        def test_ramp_up_is_90_percent_of_ramp_safety(self):
            args = parse_args(["takedown", "--url", "https://example.com", "-c", "10", "-m", "5", "-y"])
            config = build_config_from_args(args)
            assert config.ramp_up == pytest.approx(TAKEDOWN_RAMP_SAFETY * 0.9)


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

    def test_report_shows_takedown_outcome(self):
        from stress_my_site import TakedownOutcome

        stats = RunStats(url="https://example.com")
        stats.total_requests = 10
        stats.duration = 5.0
        outcome = TakedownOutcome(
            hold_minutes=5.0,
            breaking_point_found=True,
            concurrency_at_break=42,
            escalations=3,
            final_concurrency=90,
        )

        report = build_report(stats, None, takedown_outcome=outcome)
        assert "held down for 5.0 minute" in report
        assert "Recoveries observed" in report
        assert "3" in report
        assert "90" in report

    def test_report_shows_takedown_never_broke(self):
        from stress_my_site import TakedownOutcome

        stats = RunStats(url="https://example.com")
        stats.total_requests = 10
        stats.duration = 5.0
        outcome = TakedownOutcome(hold_minutes=5.0, breaking_point_found=False)

        report = build_report(stats, None, takedown_outcome=outcome)
        assert "never broke" in report
