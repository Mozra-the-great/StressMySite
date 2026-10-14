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

- CI: `ci.yml` (required check `build`: `ruff check` plus `pytest` on Python
  3.10 and 3.13), `workflow-lint.yml` (actionlint, zizmor), `secret-scan.yml`
  (gitleaks over the PR diff); `tests/test_cli_smoke.py` runs `--help` for the
  CLI and every subcommand; `ruff.toml` and `requirements-dev.txt`.

### Changed

- Documented minimum Python version is now 3.10 (`aiohttp>=3.14.4` requires
  it); `CLAUDE.md` and `README.md` previously still said 3.9+.
- Claude workflows synced with the hardened `repo-template-public` versions
  (skip cleanly without the `CLAUDE_CODE_OAUTH_TOKEN` secret; the review check
  fails on findings); Dependabot got a 7-day cooldown and commit prefixes.

### Security

- `claude.yml`: the job only starts for `@claude` mentions from an `OWNER`,
  `MEMBER` or `COLLABORATOR` (job-level `author_association` gate), so drive-by
  mentions on a public repo cannot start a run. Restores the gate from PR #14
  that the template sync (#36) had dropped.
- `requirements.txt`: bumped the `aiohttp` floor from `>=3.9` to `>=3.10.11`
  to exclude versions vulnerable to CVE-2024-23829 and CVE-2024-52304
  (HTTP request-smuggling via the response parser).
