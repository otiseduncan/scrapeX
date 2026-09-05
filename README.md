# ScrapeX v0.5.0

ScrapeX is a standalone deterministic ADAS Map automation worker:

`Calibration IQ weekly queue → managed Work Chrome ADAS Map → verified CIQ reconciliation`

Calibration IQ owns which repair orders are worked. ADAS Map is authoritative for
VIN, vehicle identity, inspection identity, and required calibrations. ScrapeX fails
closed whenever an exact row, inspection, detail page, or requirement structure cannot
be proved.

## Current scope

ScrapeX automates only ADAS Map in this release.

- It operates the already-authenticated managed **Work Chrome** window through Windows
  UI Automation.
- It never launches a second ADAS Map profile and never reads Chrome cookies, passwords,
  credential stores, or profile databases.
- It binds `RO → expanded inspection row → exact View control`, proves navigation, and
  parses only the selected Required result structure inside the webpage Document tree.
- It writes VIN/vehicle corrections and calibration additions/reactivations through the
  Calibration IQ operator API only when every receipt and authoritative reread verifies.
- One failed RO is checkpointed for operator attention while the sequential batch continues.

**ADAS SI and ALLDATA are frozen/manual-future work.** ScrapeX does not open or automate
ALLDATA, capture procedures, index documents, or write to ADAS SI in the current scope.
The dormant legacy helpers remain for compatibility but are not exposed by the runtime API.

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
and ADAS Map process-one/start/pause operations. `/api/browser/status` reports ALLDATA as
`frozen/manual_future`; there is no browser-launch or ALLDATA mutation endpoint.

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

No ALLDATA or ADAS SI live action belongs to this acceptance run.

## Git bootstrap

`scripts\init-github.ps1` initializes `main` when needed and shows a dry-run staging preview.
It never stages, commits, or pushes automatically. Review every candidate path, stage only
intentional source, then create the private remote and push without force.
