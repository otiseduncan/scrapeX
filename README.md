# ScrapeX v0.5.0

ScrapeX is a standalone browser/evidence worker with two deliberately separate session paths:

- deterministic ADAS Map acquisition through the already-authenticated managed Work Chrome session;
- agentic service-information navigation through ScrapeX Navigator provider sessions.

`Calibration IQ → ADAS Map authority / SI research task → ScrapeX → verified evidence`

Calibration IQ owns which repair orders are worked. ADAS Map is authoritative for
VIN, vehicle identity, inspection identity, and required calibrations. ScrapeX fails
closed whenever an exact row, inspection, detail page, or requirement structure cannot
be proved.

## Current scope

ScrapeX keeps ADAS Map and service-information research separate because they use
different authenticated browser contexts.

- ADAS Map operates the already-authenticated managed **Work Chrome** window through Windows
  UI Automation.
- It never launches a second ADAS Map profile and never reads Chrome cookies, passwords,
  credential stores, or profile databases.
- It binds `RO → expanded inspection row → exact View control`, proves navigation, and
  parses only the selected Required result structure inside the webpage Document tree.
- It writes VIN/vehicle corrections and calibration additions/reactivations through the
  Calibration IQ operator API only when every receipt and authoritative reread verifies.
- One failed RO is checkpointed for operator attention while the sequential batch continues.

- ALLDATA/SI research runs through the task-based Navigator with a persistent provider
  profile, accessibility/DOM observations, task-bound annotated screenshots for X Omni's
  multimodal model, opaque element refs, backtracking/loop state, and deterministic
  post-navigation verification.
- Credentials remain outside model context. The Navigator operates the authenticated
  browser session and exposes page state, not stored secrets.
- The old ALLDATA batch runner remains retired/frozen; it is not the live SI path.

## Local-only security boundary

The dashboard defaults to `http://127.0.0.1:8125`. ScrapeX has no authenticated remote
listener, so configuration and the launcher reject non-loopback bind addresses. Runtime
SQLite state, logs, captures, browser profiles, credentials, and downloaded material are
ignored and must never be committed.

Prefer a dedicated scoped `SCRAPEX_CIQ_TOKEN`. Existing read-only token discovery from the
local Calibration IQ project remains as a compatibility fallback; tokens are never printed.

## Install or repair

Use a standalone Python 3.11 through 3.14 installation:

```powershell
Set-Location 'X:\ScrapeX'
.\scripts\install.ps1
```

The installer creates `.venv`, resolves against `constraints.txt`, verifies dependencies,
and runs the tests. It does not borrow an interpreter from another repository and does not
install or launch an ALLDATA browser.

Copy `.env.example` to `.env` only when local overrides are needed. The real `.env` is
ignored; never place secrets in the example.

## Start

```powershell
.\scripts\start.ps1
```

The normal workflow is:

1. Preview/import the active Calibration IQ queue or create a bounded exact-RO batch.
2. Open the authenticated ADAS Map page in managed Work Chrome.
3. Use **Process one** for staged acceptance ROs.
4. Run the full ADAS Map batch only after the one-RO and three-shop proofs pass.
5. Review the truthful ready/attention summary and exact CIQ mutation receipts.

The production API exposes health/status, CIQ preview/import, batch CRUD/summary/exceptions,
ADAS Map process-one/start/pause operations, and task-based Navigator endpoints for dynamic
SI research. `/api/browser/status` reports the Navigator separately from the retired
legacy ALLDATA batch runner.

## Validation

Unit tests never launch a browser or live batch:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
.\.venv\Scripts\python.exe -m compileall -q .\scrapex
```

Live acceptance order is strict:

1. One exact operator-supplied CIQ RO.
2. One Macon, one Warner Robins, and one Perry RO.
3. The complete weekly ADAS Map batch.

ADAS Map acceptance remains separate from SI Navigator acceptance so the work-profile
session is never conflated with the SI provider session.

## Git bootstrap

`scripts\init-github.ps1` initializes `main` when needed and shows a dry-run staging preview.
It never stages, commits, or pushes automatically. Review every candidate path, stage only
intentional source, then create the private remote and push without force.
