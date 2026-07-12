# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `stress_my_site.py`: async HTTP load/stress tester (`asyncio` + `aiohttp`).
  Supports fixed request-count or duration-based load, optional linear
  ramp-up (duration mode only), a capped token-bucket `--rps` limiter, and
  per-second bucketed breaking-point detection that reports the approximate
  concurrency/throughput at which the target starts failing based on a
  sustained rise in hard failures (timeouts/5xx/connection errors — 4xx is
  excluded) or p95 latency. Includes a mandatory authorization confirmation
  before any run.
- Unit tests (`tests/test_stats.py`) for the pure helper functions (URL
  normalization, percentile math, ramp-up scheduling, breaking-point
  detection, report formatting, rate limiter, config validation).
