# StressMySite

> Async HTTP load/stress tester: finds the load level at which your own server starts failing.

## Type
CLI tool (HTTP load/stress tester).

## Stack
Python 3.9+, `asyncio` + `aiohttp` for concurrent request generation, `pytest` for tests. No web framework, no database, no build step.

## Structure
```
stress_my_site.py   - entire tool: pure helpers, async load generator, CLI
tests/
  test_stats.py      - unit tests for the pure helpers (no network involved)
requirements.txt     - runtime dependency (aiohttp)
```

## Commands
```sh
pip install -r requirements.txt
python stress_my_site.py                                          # interactive prompts
python stress_my_site.py break --url https://example.com -c 50
python stress_my_site.py requests --url http://localhost:8080 --target-rps 500
python stress_my_site.py takedown --url http://localhost:8080 -m 5
python -m pytest tests/ -v                                         # run unit tests
```

## Notes
- **Three subcommands, not a flat flag surface.** `break` ramps concurrency up
  until the target starts failing and reports the breaking point.
  `requests` ramps concurrency up until *measured* throughput reaches
  `--target-rps`, then holds for a fixed 30s (`REQUESTS_MODE_HOLD_SECONDS`)
  to confirm it's actually sustained rather than a one-second blip. `takedown`
  ramps past the breaking point, then holds the target down for a fixed,
  user-chosen `--minutes`, escalating concurrency whenever it recovers, so the
  operator can watch their own defenses react - it always stops automatically
  after `--minutes` (never unbounded; see the dedicated `takedown` note
  below for why that boundary is load-bearing, not incidental). There is no
  flat/count mode (`-n`) or top-level `--rps` cap/`--stop-on-break`/
  `--ramp-up` anymore — those were dropped in the mode-based rewrite; see
  `README.md` for the full flag tables per mode. `--rps` (a global rate
  limiter, `TokenBucketLimiter`) still exists, but only on `break` — it
  fights the `requests`-mode controller's own throughput-driven ramp, so
  it's not exposed there.
- **Authorization is load-bearing, not decorative.** The tool refuses to run without
  confirming the target is owned/authorized (`-y`/`--yes` skips the interactive
  prompt for scripted runs — only use that flag against targets you've already
  cleared). See `README.md` for the full rationale.
- This is a *real* load generator (comparable to `ab`/`hey`/`k6`), not a browser
  simulator — requests go out directly over HTTP via `aiohttp`, so throughput is
  bounded by this machine's CPU/network, not by how many browser tabs Chrome can
  render. That ceiling is still real: very high `-c`/`--max-concurrency` values
  can saturate the *client* before the *target* buckles — watch local CPU/network
  if pushing big numbers.
- The "breaking point" detection (`find_breaking_point` in `stress_my_site.py`)
  buckets results per second and looks for a *sustained* (2+ consecutive buckets)
  spike in the **hard-failure rate** (timeouts/5xx/connection errors —
  `Bucket.hard_failure_rate`, deliberately excludes 4xx), a p95 latency spike vs.
  baseline, **or a stall** — zero requests completing in a second where workers
  were known to be active (`Bucket.active_load > 0`), for longer than
  `max(2x baseline p95 latency, 1s)`. The stall check exists because buckets
  used to be dropped from consideration entirely once they had zero completed
  requests, so a *total* stall (every worker wedged at once — realistic at low
  concurrency, e.g. ~10 workers, when a real slowdown hits) could never
  accumulate the sustained-window count and was invisible. The baseline-relative
  threshold (not "any empty second") avoids false-positiving on targets that are
  merely slow-but-healthy (e.g. multi-second response times). `active_load` on a
  bucket is now sampled every second by `LoadGenerator._heartbeat`, independent
  of whether a request completed — previously only `_worker` touched it, and
  only when *starting* a request, so a fully-stalled second never got an
  `active_load` value written to it at all.
- **`requests` mode's ramp is feedback-driven, not time-scheduled** — unlike
  `break`'s pre-computed `ramp_delays_from_floor` schedule,
  `LoadGenerator._run_requests` spawns additional workers on demand once a
  second, based on each closed bucket's *measured* req/s vs. `--target-rps`
  (`next_concurrency` in `stress_my_site.py` - proportional growth, capped at
  2x per step, always advances by >=1 worker). It freezes once measured
  throughput reaches the target or the ceiling is hit, then holds
  (`REQUESTS_MODE_HOLD_SECONDS`) and re-checks `find_breaking_point` against
  just the hold window's buckets to produce the sustained/not-sustained
  verdict.
- **`-c`/`--concurrency` is the ramp floor, `--max-concurrency` is the
  ceiling** in all three modes - always set (auto-defaulted if the user omits
  it): `default_break_max_concurrency` (200x `-c`) for `break` and `takedown`,
  `default_requests_max_concurrency` (a rough `--target-rps`-derived estimate,
  assuming ~50 req/s/worker with 3x headroom) for `requests`. All defaults
  are printed at run start along with the assumption behind them, and are
  always overridable.
- **Very high `--max-concurrency` can make the *client* look like the
  breaking point, not the target** — on Windows especially, ephemeral ports
  (~16k by default) and TIME_WAIT exhaust well before tens of thousands of
  concurrent connections. `build_report` distinguishes this in the report:
  `stats.error_counts` holds only exceptions (timeouts, connection errors),
  while real server 5xx responses land in `stats.status_counts` instead - if
  non-timeout exceptions outnumber 5xx responses at the breaking point, a
  caveat line is appended pointing at client-side resource exhaustion instead
  of the target actually failing.
- `break` mode always stops as soon as `find_breaking_point` fires on the live
  buckets (`_progress_reporter`, checked once a second) — there's no opt-in
  flag for this anymore, since finding that point *is* the mode's purpose.
  `-d`/`--duration` (default 300s) is just the outer safety cap in case the
  target never breaks.
- **`takedown` mode (`LoadGenerator._run_takedown`) exists specifically as a
  *bounded* alternative to an earlier, rejected unbounded design.** The
  original ask was "keep the server down indefinitely, escalate whenever it
  recovers, stop only manually" — that's a recovery-defeating denial-of-service
  primitive with no measurement or stop condition, not a load test, and was
  refused as such (see project history/PR discussion). What's implemented
  instead: ramp like `break` until a breaking point, freeze concurrency at
  exactly that level (cancelling any not-yet-started ramp workers via
  `worker.cancel()` so it doesn't keep climbing past the break), then hold for
  a **mandatory, user-supplied `--minutes`**, escalating via
  `escalate_concurrency` (+25%/step, always advances by >=1, clamped to
  `--max-concurrency`) each time `find_breaking_point` on the trailing 5
  buckets returns `None` (i.e. no sustained badness recently = "recovered").
  It always stops automatically once the hold window elapses; there is
  intentionally no flag or code path to make the hold unbounded. If asked to
  remove or bypass that automatic stop, don't — surface the request instead,
  same reasoning as the original refusal.
- `normalize_url` deliberately does not use `urlparse().scheme` to detect
  whether a scheme is present — `urlparse("localhost:8080")` misreads
  `localhost` as the scheme, so the common homelab `host:port` case would
  silently pass through without `https://`/`http://` and fail in aiohttp. It
  checks for a literal `scheme://` prefix via regex instead.
- No env vars, no deployment — this is a local CLI script.

---

## Working rules

These rules apply regardless of what's above. They mirror the operator's global
Claude Code configuration so this repo behaves consistently even if opened on a
different machine or by a collaborator.

### Language
- Chat responses: German
- Everything that goes into the repo — code, comments, commit messages, branch
  names, PR descriptions, CHANGELOG: English (default, more compact)
- Respect a project's already-established language convention if one exists

### Approach — get it right once instead of fixing it ten times
1. Fully understand the task before starting
2. Think through the solution: edge cases, error handling, dependencies, blast radius
3. Only then implement it completely and correctly

No placeholders. No `// TODO` without a resolution attached. No half-finished
implementations.

For larger tasks: use Plan Mode before writing code. If something is fundamentally
unclear and would change the direction of the solution, ask once rather than
building in the wrong direction.

### Coding rules
- TypeScript: no `any`, explicit types
- Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`
- Follow the project's existing conventions over these defaults when they conflict

### Git workflow
- Never commit directly to `main`/`master` — always a feature branch
- Branch names in English, kebab-case (`feat/...`, `fix/...`)
- Before every push: check `git status` and `git diff --staged`
- No `git push --force` on main/master

### Security
- `.env`, `.env.*`, `*.pem`, `*.key`, `*.secret` — never read, log, or commit
- No credentials/tokens/passwords in code
- Secrets via environment variables or a secrets manager, never in plaintext

### Subagents — delegate proactively
Use these specialists without being asked each time, as soon as the situation fits:

- **test-runner** → right after writing or changing application code
- **security-reviewer** → after auth/input/dependency changes, and before any release
- **code-reviewer** → before a merge / after finishing a feature
- **debugger** → as soon as a test fails, a stack trace shows up, or behavior is unexpected

Run independent analyses (e.g. security + tests) in parallel. Skip subagents for
trivial changes — the overhead isn't worth it.
