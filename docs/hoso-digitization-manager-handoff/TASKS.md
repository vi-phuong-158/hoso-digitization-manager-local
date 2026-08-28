# Implementation Tasks

Sau mỗi phase: chạy test, sửa lỗi, commit, append `WORKING_LOG.md`.

## Phase 0 — Baseline

Inspect repo, xác định pipeline/taxonomy/ledger/manifest/tests/packaging, chạy baseline tests, ghi `BASELINE.md`.

Gate: `PHASE_0_BASELINE_CAPTURED`

## Phase 1 — Skeleton

FastAPI, config, SQLite, bootstrap/migration, health, templates/static, tests.

Gate: `PHASE_1_APP_SKELETON_PASS`

## Phase 2 — Taxonomy adapter

Reuse taxonomy chính thức, fallback loader, priority mapping, tests.

Gate: `PHASE_2_TAXONOMY_PASS`

## Phase 3 — Filesystem scanner

Folder/file parser, incremental, missing reconciliation, checksum, warnings, scan runs, hard-case tests.

Gate: `PHASE_3_SCANNER_PASS`

## Phase 4 — Status/progress

Checklist, overrides, progress, status engine, history, invariant tests.

Gate: `PHASE_4_STATUS_ENGINE_PASS`

## Phase 5 — UI

Dashboard, list/filter, detail, checklist actions, warnings, settings, open file/folder.

Gate: `PHASE_5_UI_PASS`

## Phase 6 — Pipeline integration

Read-only adapters cho manifest/ledger dựa trên schema thực; fallback Noop.

Gate: `PHASE_6_INTEGRATION_PASS`

## Phase 7 — E2E & fixtures

Synthetic fixture tree + Playwright/smoke.

Gate: `PHASE_7_E2E_PASS`

## Phase 8 — Windows packaging

PyInstaller, launcher, local assets, writable db/log/config, build docs.

Gate: `PHASE_8_WINDOWS_PACKAGE_PASS`

## Phase 9 — Pilot readiness

Full suite, regression, smoke, source-hash verification, representative scan benchmark, final report.

Final: `DIGITIZATION_MANAGER_MVP_READY_FOR_LOCAL_PILOT`
