# Dashboard W11 local browser smoke

The optional Playwright smoke starts the dashboard against a temporary data
root and runs Chromium at a 390 × 844 viewport. Run it with Python, Playwright,
and a system Chromium executable available:

```sh
python tests/test_webapp_browser.py
```

Local run on 2026-10-13: all 4 browser tests passed with Python 3.12.14,
Playwright 1.62.0, and Chromium 154.0.8037.57.

The smoke checks that the Library renders its fixture video and stays within
the narrow viewport, Ask reports no evidence without inventing a citation,
budget-blocked and lease-recovery-unknown states remain visible, and Digest
requires both checkbox and dialog confirmation before a send task is queued.
The browser context aborts every non-loopback request. Ask keyword extraction
is stubbed and the final model call is asserted absent. Digest preview must
report zero remote calls, and the test never starts a digest worker; it only
verifies the queued task.

This is local Chromium evidence for the listed UI flows. It does not satisfy a
hosted-run/browser deployment gate or any remaining product acceptance. The
ordinary test suite skips this file when Playwright or Chromium is absent.
