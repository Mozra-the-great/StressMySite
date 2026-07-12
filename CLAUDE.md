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
python stress_my_site.py                                   # interactive prompts
python stress_my_site.py --url https://example.com -c 50 -n 5000
python stress_my_site.py --url http://localhost:8080 -c 100 -d 30 --ramp-up 20
python -m pytest tests/ -v                                  # run unit tests
```

## Notes
- **Authorization is load-bearing, not decorative.** The tool refuses to run without
  confirming the target is owned/authorized (`-y`/`--yes` skips the interactive
  prompt for scripted runs — only use that flag against targets you've already
  cleared). See `README.md` for the full rationale.
- This is a *real* load generator (comparable to `ab`/`hey`/`k6`), not a browser
  simulator — requests go out directly over HTTP via `aiohttp`, so throughput is
  bounded by this machine's CPU/network, not by how many browser tabs Chrome can
  render. That ceiling is still real: very high `-c`/`--rps` values can saturate
  the *client* before the *target* buckles — watch local CPU/network if pushing
  big numbers.
- The "breaking point" detection (`find_breaking_point` in `stress_my_site.py`)
  buckets results per second and looks for a *sustained* (2+ consecutive buckets)
  spike in the **hard-failure rate** (timeouts/5xx/connection errors —
  `Bucket.hard_failure_rate`, deliberately excludes 4xx) or p95 latency vs.
  baseline. It's most useful paired with `--ramp-up`, since a gradually
  increasing load surfaces the load level at which things start to break
  rather than an instant on/off signal.
- `--ramp-up` only works in duration mode (`-d`). In count mode (`-n`) all
  workers drain one shared request budget, so workers started immediately
  exhaust it before staggered ones ever wake up — the ramp silently never
  happens. `build_config_from_args` rejects the combination outright rather
  than silently ignoring `--ramp-up`.
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
