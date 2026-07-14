# StressMySite

An async HTTP load/stress tester that generates real concurrent traffic against
a target and reports throughput, latency percentiles, and — most importantly —
the approximate load level at which the target starts to fail (its "breaking
point").

This is a genuine load generator, comparable in spirit to `ab`, `hey`, or `k6`:
requests go out directly over HTTP using `asyncio` + `aiohttp`, so throughput is
bounded by your machine's CPU/network, not by how many browser tabs a browser
can render.

## ⚠️ Only test what you own or are authorized to test

This tool can generate enough concurrent load to degrade or take down a
service. Running it against a target you don't own or don't have explicit
permission to test is unauthorized and, depending on jurisdiction, illegal
(e.g. unauthorized access / denial-of-service laws). Every run — interactive
or scripted (`--yes`) — is your explicit confirmation that you have that
authorization. Use it against your own servers, staging environments, or
targets you have written permission to test.

`takedown` mode specifically holds a target down on purpose, for a
fixed, operator-chosen window, so you can watch your own defenses react - it
is not, and must never be used as, an unbounded denial-of-service tool. It
always stops automatically once `--minutes` elapses.

## Install

Requires Python 3.9+ and Chrome/Chromium is **not** needed (there's no browser
involved).

```sh
pip install -r requirements.txt
```

## Three modes

```sh
python stress_my_site.py break    --url <target> [options]
python stress_my_site.py requests --url <target> --target-rps N [options]
python stress_my_site.py takedown --url <target> --minutes N [options]
python stress_my_site.py                          # interactive: prompts for everything
```

**`break`** ramps concurrency up until your target starts failing, and reports
the breaking point: the first sustained window where the failure rate, p95
latency, or throughput itself (see "Detecting a stall" below) crosses a
threshold. Use this when the question is "where does this fall over?"

**`requests`** ramps concurrency up until *measured* throughput reaches
`--target-rps`, then holds there for 30s to confirm the rate is actually
sustained rather than a one-second blip. Use this when the question is "can
this handle N requests/second?"

**`takedown`** ramps past the breaking point, then holds the target down for
a fixed number of minutes - escalating concurrency whenever it recovers - so
you can watch your own defenses (rate limiting, autoscaling, alerting) react
in real time while it runs, then get a summary of how they held up. Use this
when the question is "if this got hit hard right now, would my defenses catch
it?" It always stops automatically once `--minutes` elapses (or immediately
on Ctrl+C) - there is no unbounded mode.

All three modes ramp from a floor (`-c`/`--concurrency`) toward a ceiling
(`--max-concurrency`), but `break`/`takedown` and `requests` ramp
differently: `break` and `takedown`'s initial ramp follow a pre-computed,
time-based schedule (concurrency grows on a fixed clock, independent of what's
actually happening). `requests` is feedback-driven — it looks at each closed
second's *measured* req/s and only grows concurrency while that's under
target, freezing the moment it's reached (or the ceiling is hit). `takedown`
switches to a feedback-driven escalation once it breaks: concurrency freezes
at the level that broke the target, then only grows again when the target is
observed to have recovered.

## Usage

### Interactive

```sh
python stress_my_site.py
```

You'll be prompted for the target URL, mode (`break`/`requests`/`takedown`),
the concurrency floor, the mode's headline value (nothing further for
`break`; `--target-rps` for `requests`; `--minutes` for `takedown`), and an
authorization confirmation. Anything not prompted for (timeouts, ceilings,
etc.) uses the same defaults as the command-line form below.

### `break` mode

```sh
# Ramp from 50 upward (default ceiling: 200x -c = 10,000) for up to 300s
# (the default safety cap), stopping as soon as it breaks
python stress_my_site.py break --url https://example.com -c 50

# Explicit ceiling and a tighter safety cap
python stress_my_site.py break --url http://localhost:8080 \
    -c 100 --max-concurrency 20000 -d 120
```

| Flag | Description |
|---|---|
| `--url` | Target URL, domain, or bare IP (scheme defaults to `https://`) |
| `-c`, `--concurrency` | Ramp floor - starting concurrent connections (default: 10) |
| `--max-concurrency` | Ramp ceiling. Defaults to `200 x -c` if omitted - a generous ceiling since finding it *is* the point |
| `-d`, `--duration` | Outer safety cap in seconds, in case the target never breaks (default: 300) |
| `--rps` | Optional global rate limit (requests/second) across all workers |
| `--timeout` | Per-request timeout in seconds (default: 10) |
| `--method` | HTTP method (default: `GET`) |
| `--insecure` | Disable TLS certificate verification (e.g. self-signed certs on a homelab target) |
| `-y`, `--yes` | Skip the interactive authorization prompt |

The ramp climbs across ~90% of `--duration`, leaving a tail at full
concurrency so the top of the climb gets a chance to send something before
the run ends. The run always stops the moment a breaking point is detected -
there's no reason to keep hammering an already-broken target for the rest of
`--duration`.

### `requests` mode

```sh
# Ramp up until throughput reaches 500 req/s, then hold for 30s
python stress_my_site.py requests --url http://localhost:8080 --target-rps 500

# Cap how high the ramp is allowed to climb while chasing the target
python stress_my_site.py requests --url http://localhost:8080 \
    --target-rps 5000 --max-concurrency 300
```

| Flag | Description |
|---|---|
| `--url` | Target URL, domain, or bare IP |
| `--target-rps` | Target requests/second to ramp toward and hold (required) |
| `-c`, `--concurrency` | Ramp floor - starting concurrent connections (default: 10) |
| `--max-concurrency` | Ramp ceiling. Defaults to a rough estimate from `--target-rps` (assumes ~50 req/s per worker, x3 headroom) - override this if your target is slower or faster than that assumption |
| `--timeout` | Per-request timeout in seconds (default: 10) |
| `--method` | HTTP method (default: `GET`) |
| `--insecure` | Disable TLS certificate verification |
| `-y`, `--yes` | Skip the interactive authorization prompt |

There's no `--duration` here: the run length is however long it takes to
reach the target (or give up at the ceiling), plus a fixed 30s hold. If the
measured rate degrades during the hold (errors, latency spike, or a stall -
see below), the run reports that the rate was **not sustained** and shows
the breaking point within the hold window.

### `takedown` mode

```sh
# Break it, then hold it down for 5 minutes so you can watch your
# alerting/autoscaling react in real time
python stress_my_site.py takedown --url http://localhost:8080 -m 5
```

| Flag | Description |
|---|---|
| `--url` | Target URL, domain, or bare IP |
| `-m`, `--minutes` | Minutes to hold the target down for once it breaks (required) |
| `-c`, `--concurrency` | Ramp floor - starting concurrent connections (default: 10) |
| `--max-concurrency` | Optional escalation ceiling. **Unset by default** - escalation during the hold phase then has no cap other than this machine's own resources (see "A note on limits" below). Pass this if you want a hard ceiling instead |
| `--timeout` | Per-request timeout in seconds (default: 10) |
| `--method` | HTTP method (default: `GET`) |
| `--insecure` | Disable TLS certificate verification |
| `-y`, `--yes` | Skip the interactive authorization prompt |

`takedown` first ramps exactly like `break` (using an internal 200x-`-c`
target for that initial search, regardless of `--max-concurrency` - that's
just how far the ramp searches for a breaking point, a separate concept from
the hold phase below). The moment a breaking point is detected, concurrency
is frozen at exactly that level (any workers still waiting to start as part
of the ramp schedule are cancelled) - the run then holds there for
`--minutes`, checking the target's health once a second against the trailing
few seconds of buckets. Whenever the target is observed to have recovered,
concurrency is escalated by 25% (always at least one more worker) to push
past whatever just happened - clamped to `--max-concurrency` if you passed
one, otherwise unbounded.

**The time window is always bounded, the concurrency isn't (unless you ask
it to be).** There's no `--duration` and no unbounded *time* option: the
whole point is a fixed, operator-chosen window so you can run this against
your own staging/homelab target and actively watch (in another terminal,
dashboard, alert channel, etc.) whether your defenses respond in time - not
to keep a target down indefinitely. Concurrency, on the other hand, defaults
to escalating as far as it needs to rather than stopping at a guessed
number - pass `--max-concurrency` explicitly if you want to cap that too
(e.g. to protect your own machine's resources, or to simulate a specific
attacker capacity).

## Detecting a stall

All three modes report a **breaking point**: the first sustained window (2+
consecutive one-second buckets) where either the hard-failure rate (timeouts,
5xx responses, and connection errors — 4xx responses are deliberately
excluded, since an endpoint that legitimately rejects requests with e.g. 404
isn't "failing under load") exceeds 5%, p95 latency exceeds 3x the baseline,
**or the target has gone quiet** — zero requests completing in a second while
workers were known to be active, for longer than `max(2x baseline latency,
1s)`.

That last condition matters at low concurrency specifically: if only ~10
workers are in flight and the target chokes, it's entirely possible for
*every* worker to stall simultaneously, producing whole seconds with zero
completions. The gap is measured against the baseline latency (not "any
empty second") so a target with multi-second response times doesn't
false-positive just for being slow-but-healthy.

If found, you get something like:

```
BREAKING POINT DETECTED:
  Server started failing around t=14s, active load ~68.
  Reason: failure rate 12.0% exceeded 5% threshold
  Failure rate at that point: 12.0%, p95 latency: 0.842s
```

If the target held up under the full applied load, you'll see "No breaking
point detected" instead — try a higher `-c`/`--max-concurrency` to push
further.

## Example reports

### `break`

```
============================================================
Target:            https://example.com
Duration:          30.02s
Total requests:    18420
Successful:        17103 (92.8%)
Failed:            1317 (7.2%)
Throughput:        613.7 req/s

Status codes:
  200: 17103
  503: 1317

Latency (seconds):
  min:  0.0120
  avg:  0.1840
  p50:  0.1420
  p90:  0.3910
  p95:  0.5220
  p99:  0.9840
  max:  2.1030

Per-second breakdown (time / active load / req/s / success rate / p95):
  t=   0s  load=    5  req/s=    98  success=100.0%  p95=0.045s
  ...
  t=  14s  load=   68  req/s=   601  success= 88.0%  p95=0.842s
  ...

BREAKING POINT DETECTED:
  Server started failing around t=14s, active load ~68.
  Reason: failure rate 12.0% exceeded 5% threshold
  Failure rate at that point: 12.0%, p95 latency: 0.842s
============================================================
```

### `requests`

```
============================================================
Target:            http://localhost:8080
Duration:          38.00s
Total requests:    17820
...

Target rate:       500.0 req/s
Reached:           ~512.3 req/s at concurrency 14
Sustained for the full 30s hold window.

No breaking point detected - target held up under the applied load.
============================================================
```

### `takedown`

```
============================================================
Target:            http://localhost:8080
Duration:          312.40s
Total requests:    1284031
...

Broke at concurrency ~68, held down for 5.0 minute(s).
Recoveries observed (each triggered an escalation): 4
Final concurrency:  166

BREAKING POINT DETECTED:
  Server started failing around t=12s, active load ~68.
  Reason: failure rate 12.0% exceeded 5% threshold
============================================================
```

## A note on limits

There's still a practical ceiling: very high concurrency can saturate *this
machine's* CPU/network before the target actually buckles. If throughput
plateaus while your own CPU sits near 100%, that's the client, not the
server, being the bottleneck — reduce `-c`/`--max-concurrency` and/or spread
the load across multiple machines rather than trusting the numbers as-is.

This matters even more once `--max-concurrency` climbs into the thousands:
on Windows, the default ephemeral port range is ~16,000 ports, plus a
TIME_WAIT hold-open period after each connection closes — so tens of
thousands of concurrent connections can exhaust the *client's* own ports
well before the target buckles. If the report's breaking point is dominated
by connection errors rather than server 5xx responses, it prints a caveat
to that effect — treat that as "the client ran out of resources," not "the
target broke," and either lower `--max-concurrency` or spread the load
across multiple machines.

**`takedown` without `--max-concurrency` deliberately has no ceiling on
this**, since the whole point is escalating as far as it takes to outpace a
recovering defense. That means it's the one place in this tool where the
above client-side limits aren't just a caveat to watch for - they're the
*only* remaining cap once you drop `--max-concurrency`. If you'd rather
have a hard ceiling than find out where your own machine's limit is,
pass `--max-concurrency` explicitly.

## Tests

```sh
pip install pytest
python -m pytest tests/ -v
```

Tests cover the pure helper functions (URL normalization, percentile math,
ramp-up scheduling, the adaptive concurrency controller, breaking-point
detection, CLI argument validation, report formatting) — no network or
asyncio event loop involved.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
