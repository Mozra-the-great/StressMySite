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

## Install

Requires Python 3.9+ and Chrome/Chromium is **not** needed (there's no browser
involved).

```sh
pip install -r requirements.txt
```

## Usage

### Interactive

```sh
python stress_my_site.py
```

You'll be prompted for the target URL, concurrency, load mode (fixed request
count vs. time-based duration), and an authorization confirmation.

### Command-line

```sh
# 5,000 requests total, 50 concurrent connections
python stress_my_site.py --url https://example.com -c 50 -n 5000

# Sustain load for 30 seconds with a 20-second ramp-up (0 -> 100 connections)
python stress_my_site.py --url http://localhost:8080 -c 100 -d 30 --ramp-up 20

# Keep climbing from 100 up to 20,000 concurrent connections across the whole
# 5-minute run, stopping as soon as a breaking point is detected
python stress_my_site.py --url http://localhost:8080 -c 100 -d 300 \
    --max-concurrency 20000 --stop-on-break
```

| Flag | Description |
|---|---|
| `--url` | Target URL, domain, or bare IP (scheme defaults to `https://`) |
| `-c`, `--concurrency` | Number of concurrent connections (the ramp's starting point if `--max-concurrency` is set) |
| `-n`, `--requests` | Total number of requests to send (count mode) |
| `-d`, `--duration` | Duration in seconds to sustain load (duration mode) — mutually exclusive with `-n` |
| `--ramp-up` | Seconds to linearly ramp concurrency up (default: 0 = instant full load; defaults to ~90% of `--duration` if `--max-concurrency` is given). Only supported in duration mode (`-d`) — see below |
| `--max-concurrency` | Ceiling to continuously ramp concurrency up to over the run, instead of plateauing at `-c` once the ramp finishes. Requires `-d`/`--duration`. Use this to push toward tens of thousands of req/s |
| `--stop-on-break` | End the run as soon as a breaking point is detected, instead of always running the full `-d`/`-n` |
| `--timeout` | Per-request timeout in seconds (default: 10) |
| `--method` | HTTP method (default: `GET`) |
| `--insecure` | Disable TLS certificate verification (e.g. self-signed certs on a homelab target) |
| `--rps` | Optional global rate limit (requests/second) across all workers |
| `-y`, `--yes` | Skip the interactive authorization prompt (for scripted runs against already-cleared targets) |

You must specify either `-n`/`--requests` (count mode) or `-d`/`--duration`
(duration mode) — not both. If neither is given on the command line, you'll be
prompted to choose interactively.

## Ramp-up and the breaking point

Starting at full concurrency instantly tells you whether the target survives
*that* load level — it doesn't tell you the boundary. With `--ramp-up`,
concurrency increases linearly over the given number of seconds, and the tool
buckets results per second, correlating error rate / timeouts / p95 latency
with the concurrency active at that moment.

`--ramp-up` only works with `-d`/`--duration`, not `-n`/`--requests`: in count
mode all workers share one request budget, and workers that start immediately
drain it before staggered workers ever wake up, so the ramp never actually
happens. Combining `--ramp-up` with `-n` is rejected with an explanation.
`--ramp-up` must also be less than `--duration` — otherwise workers are still
staggering in when the run ends and the ramp never reaches full concurrency.

By default the ramp climbs from `-c` up to `-c` (i.e. no further scaling once
it plateaus) — that ceiling is exactly `-c`, no more. To keep climbing for the
*whole* run instead of flattening out early, add `--max-concurrency`: the ramp
then targets that value instead, defaulting `--ramp-up` to ~90% of
`--duration` so it's still climbing right up to near the end. This is the way
to reach req/s in the thousands or tens of thousands, since `-c` alone is a
hard ceiling regardless of `--ramp-up`. Pair it with `--stop-on-break` so the
run ends automatically once the target starts failing, instead of continuing
to hammer an already-broken server for the rest of `--duration`.

After the run, it reports whether it found a **breaking point**: the first
sustained window (2+ consecutive one-second buckets) where either the
hard-failure rate (timeouts, 5xx responses, and connection errors — 4xx
responses are deliberately excluded, since an endpoint that legitimately
rejects requests with e.g. 404 isn't "failing under load") exceeds 5%, or p95
latency exceeds 3x the baseline. If found, you get something like:

```
BREAKING POINT DETECTED:
  Server started failing around t=14s, active load ~68.
  Reason: failure rate 12.0% exceeded 5% threshold
  Failure rate at that point: 12.0%, p95 latency: 0.842s
```

If the target held up under the full applied load, you'll see "No breaking
point detected" instead — try a higher `-c`/`-n`/`-d` to push further.

**Recommendation:** start with a modest concurrency and a `--ramp-up`, rather
than jumping straight to a very high `-c`. It's easier to read a gradual climb
to failure than to walk in with a sledgehammer and only learn "it fell over
immediately."

## Example report

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

## A note on limits

There's still a practical ceiling: very high concurrency or `--rps` values can
saturate *this machine's* CPU/network before the target actually buckles. If
throughput plateaus while your own CPU sits near 100%, that's the client, not
the server, being the bottleneck — reduce `-c` and/or spread the load across
multiple machines rather than trusting the numbers as-is.

This matters even more with `--max-concurrency` pushed into the thousands:
on Windows, the default ephemeral port range is ~16,000 ports, plus a
TIME_WAIT hold-open period after each connection closes — so tens of
thousands of concurrent connections can exhaust the *client's* own ports
well before the target buckles. If the report's breaking point is dominated
by connection errors rather than server 5xx responses, it prints a caveat
to that effect — treat that as "the client ran out of resources," not "the
target broke," and either lower `--max-concurrency` or spread the load
across multiple machines.

## Tests

```sh
pip install pytest
python -m pytest tests/ -v
```

Tests cover the pure helper functions (URL normalization, percentile math,
ramp-up scheduling, breaking-point detection, report formatting) — no network
or asyncio event loop involved.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
