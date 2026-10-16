# Dashboard W10 scale baseline

This baseline measures the dashboard’s paginated library and local search API with 1,000 and 10,000 synthetic indexed jobs. It is a reproducible observation of this checkout and machine, not a service-level guarantee or a pass/fail result.

## Reproduce

From the repository root, run:

```bash
python scripts/benchmark_dashboard_scale.py --output docs/dashboard-w10-results.json
```

The harness needs Linux `/proc` for process RSS sampling and the project’s normal Python dependencies. It creates a temporary library with two FTS events and one manifest per video, starts the dashboard in a separate loopback process, and removes the synthetic data when finished. It makes no provider calls and needs no API credentials.

Defaults are 30 measured serial samples and three warmups per operation, 100 results per library page, and eight bounded reader threads. Each reader performs five page/search pairs. Page queries are spread across the available pages. Search measures a term present in one out of ten videos and a term with no matches; results are capped at 20. The concurrent sample measures library pages and the matching search term.

Latency is client-observed HTTP round-trip time, including JSON decoding. P50 is the median; p95 uses nearest-rank (`ceil(0.95 × n)`). RSS is sampled from the dashboard server process every 10 ms during warmups and measurements, excluding the load generator and dataset construction. It reports the server’s starting RSS, sampled peak, and peak increase. A short-lived allocation between samples may not be observed.

## Machine and results

Measured 2026-10-13 against dashboard revision `c86c4cb7a9f80fdeb1ffc022d806cdda5edc31b8` on Linux `6.18.44`, x86_64, AMD EPYC 9V74, Python 3.12.14. The container reported five logical CPUs. The complete machine record and JSON samples are in [dashboard-w10-results.json](dashboard-w10-results.json).

Serial latency, in milliseconds:

| Synthetic videos | Library page p50 / p95 | Search hit p50 / p95 | Search miss p50 / p95 |
|---:|---:|---:|---:|
| 1,000 | 29.968 / 32.623 | 2.279 / 2.734 | 1.508 / 1.779 |
| 10,000 | 35.321 / 39.616 | 3.856 / 4.165 | 1.500 / 1.960 |

Bounded concurrent reader latency, in milliseconds (eight readers, 40 page/search pairs):

| Synthetic videos | Library page p50 / p95 | Search hit p50 / p95 |
|---:|---:|---:|
| 1,000 | 816.577 / 898.694 | 51.050 / 100.781 |
| 10,000 | 786.913 / 905.338 | 63.522 / 100.550 |

Dashboard process RSS sampled during each workload:

| Synthetic videos | Baseline RSS | Peak RSS | Peak increase |
|---:|---:|---:|---:|
| 1,000 | 22.070 MiB | 29.680 MiB | 7.609 MiB |
| 10,000 | 22.070 MiB | 42.234 MiB | 20.164 MiB |

## Thresholds and limits

W10 asks for a machine baseline before setting an acceptable performance threshold, but the issue specifies no latency, concurrency, or RSS target. There is no existing product budget from which to derive a defensible numeric limit. The threshold field is therefore `null` in the JSON report; these results make no pass/fail claim. A maintainer can set a threshold after choosing a supported machine profile and user-facing latency/resource budget.

This is an API baseline, not a browser-rendering measurement. It does not verify visual responsiveness, DOM work, mobile layout, or W11 browser workflows. The concurrent timings include local HTTP and JSON handling and should be compared only with runs using the same harness configuration and a documented machine profile.
