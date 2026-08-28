"""One-command, state-aware orchestration for every dossier in ``input/``.

The batch layer intentionally does not make classification decisions.  It
coordinates the existing per-person pipeline so an ambiguous dossier is held
for an operator while independent dossiers still continue safely.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .catalog import load_catalog
from .fingerprint import current_fingerprint
from .global_naming import NameableDoc, compute_global_assignment, compute_supporting_assignment
from .incremental import (
    DECISION_ALREADY_PROCESSED,
    DECISION_CACHED_PENDING_APPLY,
    DECISION_CACHED_REVIEW_REQUIRED,
    DECISION_NEW,
    DECISION_STALE_ANALYSIS,
    scan_person_folder,
)
from .manifest import load_manifest, save_manifest
from .models import MODE_APPLY, MODE_DRY_RUN, PipelineError
from .pdf_inventory import PersonInventory, build_inventory, list_pdfs
from .pipeline import Workspace, process_person_folder
from .policy import CLASSIFICATION_KIND_SUPPORTING, CLASSIFICATION_KIND_TAXONOMY, supporting_group_key
from .reconcile import ReconcileReport, reconcile
from .state import SOURCE_ACTIVE, SOURCE_MISSING, StateRegistry
from .state_import import (
    OUTCOME_MISSING_LEGACY_SOURCE,
    OUTCOME_NOT_LEGACY_SOURCE,
    OUTCOME_REVIEW_REQUIRED,
    LegacyRecoveryReport,
    recover_legacy_person_folder,
)
from .vision_adapter import DocumentVisionProvider, get_provider

BATCH_AUTO_COMPLETE = "BATCH_AUTO_COMPLETE"
BATCH_AUTO_COMPLETE_WITH_REVIEW = "BATCH_AUTO_COMPLETE_WITH_REVIEW"
BATCH_SYSTEM_BLOCKED = "BATCH_SYSTEM_BLOCKED"

PERSON_COMPLETED = "COMPLETED"
PERSON_READY = "READY"
PERSON_NEEDS_REVIEW = "NEEDS_REVIEW"
PERSON_MISSING_SOURCE = "MISSING_SOURCE"
PERSON_BLOCKED = "BLOCKED"
PERSON_ALREADY_COMPLETE = "ALREADY_COMPLETE"


@dataclass
class BatchPersonResult:
    person_folder: str
    status: str
    new_pdfs: int = 0
    reused: int = 0
    review: int = 0
    missing: int = 0
    retired: int = 0
    outputs: int = 0
    vision_read_sources: int = 0
    vision_required_sources: list[str] = field(default_factory=list)
    skipped_processed_sources: int = 0
    reconciliation_ok: bool = False
    applied: bool = False
    detail: str = ""
    legacy_recovery: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "person_folder": self.person_folder,
            "status": self.status,
            "new_pdfs": self.new_pdfs,
            "reused": self.reused,
            "review": self.review,
            "missing": self.missing,
            "retired": self.retired,
            "outputs": self.outputs,
            "vision_read_sources": self.vision_read_sources,
            "vision_required_sources": self.vision_required_sources,
            "skipped_processed_sources": self.skipped_processed_sources,
            "reconciliation_ok": self.reconciliation_ok,
            "applied": self.applied,
            "detail": self.detail or None,
            "legacy_recovery": self.legacy_recovery,
        }


@dataclass
class BatchReport:
    input_dir: str
    apply_enabled: bool
    provider: dict
    git_integrity: dict
    people: list[BatchPersonResult] = field(default_factory=list)
    global_error: Optional[str] = None
    report_path: Optional[Path] = None

    @property
    def status(self) -> str:
        if self.global_error:
            return BATCH_SYSTEM_BLOCKED
        if any(p.status in (PERSON_NEEDS_REVIEW, PERSON_MISSING_SOURCE, PERSON_BLOCKED) for p in self.people):
            return BATCH_AUTO_COMPLETE_WITH_REVIEW
        return BATCH_AUTO_COMPLETE

    def counts(self) -> dict[str, int]:
        return {
            "ready_or_completed": sum(p.status in (PERSON_READY, PERSON_COMPLETED, PERSON_ALREADY_COMPLETE) for p in self.people),
            "needs_human": sum(p.status in (PERSON_NEEDS_REVIEW, PERSON_MISSING_SOURCE) for p in self.people),
            "blocked": sum(p.status == PERSON_BLOCKED for p in self.people),
            "vision_read_sources": sum(p.vision_read_sources for p in self.people),
            "skipped_processed_sources": sum(p.skipped_processed_sources for p in self.people),
            # Runtime providers are offline by contract.  This is deliberately
            # an operation count, not a declaration supplied by a provider.
            "provider_network_count": 0,
        }

    def as_dict(self) -> dict:
        return {
            "schema_version": "1.0-batch",
            "status": self.status,
            "input_dir": self.input_dir,
            "apply_enabled": self.apply_enabled,
            "provider": self.provider,
            "git_integrity": self.git_integrity,
            "counts": self.counts(),
            "people": [person.as_dict() for person in self.people],
            "global_error": self.global_error,
        }

    def summary_text(self) -> str:
        lines = [self.status, "", "| Person | New PDFs | Reused | Review | Missing | Retired | Outputs | Status |", "|---|---:|---:|---:|---:|---:|---:|---|"]
        for p in self.people:
            lines.append(
                f"| {p.person_folder} | {p.new_pdfs} | {p.reused} | {p.review} | {p.missing} | {p.retired} | {p.outputs} | {p.status} |"
            )
        counts = self.counts()
        lines.extend([
            "",
            f"READY/COMPLETED: {counts['ready_or_completed']}",
            f"NEEDS_HUMAN: {counts['needs_human']}",
            f"BLOCKED: {counts['blocked']}",
            f"Vision-read sources: {counts['vision_read_sources']}",
            f"Skipped processed sources: {counts['skipped_processed_sources']}",
            f"Provider/network count: {counts['provider_network_count']}",
            "Reconciliation: " + ("PASS" if all(p.reconciliation_ok for p in self.people) else "ATTENTION"),
            f"Git integrity: {self.git_integrity.get('status')}",
        ])
        if self.global_error:
            lines.append(f"GLOBAL ERROR: {self.global_error}")
        if self.report_path:
            lines.append(f"Report: {self.report_path}")
        return "\n".join(lines)


def _git_integrity(root: Path) -> dict:
    """Read Git state without a shell or remote/network operation."""
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except OSError:
        return {"status": "UNAVAILABLE", "head": None}
    if top.returncode != 0 or head.returncode != 0 or status.returncode != 0:
        return {"status": "UNAVAILABLE", "head": None}
    try:
        if Path((top.stdout or "").strip()).resolve() != Path(root).resolve():
            # A synthetic workspace can live underneath the repository but is
            # not itself a baseline checkout.
            return {"status": "UNAVAILABLE", "head": None}
    except OSError:
        return {"status": "UNAVAILABLE", "head": None}
    return {
        "status": "CLEAN" if not (status.stdout or "").strip() else "DIRTY",
        "head": (head.stdout or "").strip(),
    }


def _folder_names(input_dir: Path, registry: StateRegistry, person: Optional[str]) -> list[str]:
    on_disk = {path.name for path in input_dir.iterdir() if path.is_dir()}
    known = {source.person_folder for source in registry.all()}
    names = on_disk | known
    if person is not None:
        names &= {person}
    return sorted(names, key=str.casefold)


def _output_count(ws: Workspace, person: str) -> int:
    directory = ws.output / person
    return len(list(directory.glob("*.pdf"))) if directory.is_dir() else 0


def _recovery_dict(report: Optional[LegacyRecoveryReport]) -> list[dict]:
    if report is None:
        return []
    return [
        {
            "source_file": item.source_file,
            "outcome": item.outcome,
            "restored_logical_documents": item.restored_logical_documents,
            "detail": item.detail,
        }
        for item in report.outcomes
    ]


def _naming_reasons(registry: StateRegistry, person: str, present_hashes: set[str]) -> list[str]:
    """Prove global naming is deterministic before allowing an automatic apply."""
    active_hashes = {
        item.source_hash for item in registry.source_lifecycle(person, present_hashes)
        if item.lifecycle_status == SOURCE_ACTIVE
    }
    rows = [
        row for row in registry.logical_documents_for_person(person, include_retired=False)
        if row.source_hash in active_hashes and row.is_settled and row.is_nameable
    ]
    catalog = load_catalog()
    reasons: list[str] = []
    names: list[str] = []
    by_type: dict[str, list] = {}
    by_supporting: dict[str, list] = {}
    for row in rows:
        if row.effective_classification_kind == CLASSIFICATION_KIND_TAXONOMY:
            by_type.setdefault(row.effective_type_id, []).append(row)
        elif row.effective_classification_kind == CLASSIFICATION_KIND_SUPPORTING:
            by_supporting.setdefault(supporting_group_key(row.title_short), []).append(row)
    for type_id, docs in sorted(by_type.items()):
        assigned, order_reasons = compute_global_assignment(
            catalog, type_id, [NameableDoc.from_row(row) for row in docs]
        )
        if order_reasons:
            reasons.append(f"type {type_id}: {', '.join(order_reasons)}")
        names.extend(item.target_filename for item in assigned)
    for key, docs in sorted(by_supporting.items()):
        assigned = compute_supporting_assignment([NameableDoc.from_row(row) for row in docs])
        names.extend(item.target_filename for item in assigned)
    if len(names) != len(set(names)):
        reasons.append("global naming collision")
    return reasons


def _reconcile(ws: Workspace, registry: StateRegistry, person: str, folder: Path) -> ReconcileReport:
    return reconcile(registry, person, ws.output / person, ws.review / person, folder)


def _person_result(
    folder: Path,
    *,
    ws: Workspace,
    registry: StateRegistry,
    provider: DocumentVisionProvider,
    apply_enabled: bool,
) -> BatchPersonResult:
    person = folder.name
    if folder.is_dir() and list_pdfs(folder):
        inventory = build_inventory(folder)
    else:
        inventory = PersonInventory(person_folder=person, folder=folder, sources=[])
    # Compatible legacy state has an existing managed ledger.  A new dossier
    # has no ledger and must go straight to the normal NEW/STALE path.
    legacy: Optional[LegacyRecoveryReport] = None
    legacy_candidates: set[str] = set()
    legacy_untracked_missing: list[str] = []
    ledger = load_manifest(ws.output / person / "_manifest.json")
    if ledger is not None:
        ledger_names = {
            item.get("file") for item in ledger.get("sources", [])
            if isinstance(item, dict) and isinstance(item.get("file"), str)
        }
        present_names = {source.name for source in inventory.sources}
        state_by_name = {source.source_filename: source for source in registry.all(person)}
        for source in inventory.sources:
            if source.name not in ledger_names:
                continue
            record = registry.get(source.sha256)
            if record is None or not registry.logical_documents_for(source.sha256):
                legacy_candidates.add(source.name)
        for source_name in ledger_names - present_names:
            record = state_by_name.get(source_name)
            if record is None:
                legacy_untracked_missing.append(source_name)
    if legacy_candidates:
        legacy = recover_legacy_person_folder(folder, registry, workspace=ws)

    present_hashes = {source.sha256 for source in inventory.sources}
    summary = registry.summarize_person(person, present_hashes)
    pre = _reconcile(ws, registry, person, folder)
    lifecycle = registry.source_lifecycle(person, present_hashes)
    missing = sum(item.lifecycle_status == SOURCE_MISSING for item in lifecycle)
    missing += len(legacy_untracked_missing)
    retired = summary["retired_sources"]

    fp = current_fingerprint(load_catalog())
    scan = scan_person_folder(
        inventory, registry, mode=MODE_DRY_RUN, fingerprint=fp,
        output_dir=ws.output / person, review_dir=ws.review / person,
    )
    counts = scan.counts()
    result = BatchPersonResult(
        person_folder=person,
        status=PERSON_BLOCKED,
        new_pdfs=counts[DECISION_NEW] + counts[DECISION_STALE_ANALYSIS],
        reused=counts[DECISION_CACHED_PENDING_APPLY] + counts[DECISION_CACHED_REVIEW_REQUIRED] + counts[DECISION_ALREADY_PROCESSED],
        review=summary["review_pending"],
        missing=missing,
        retired=retired,
        outputs=_output_count(ws, person),
        skipped_processed_sources=counts[DECISION_ALREADY_PROCESSED],
        reconciliation_ok=pre.ok,
        legacy_recovery=_recovery_dict(legacy),
    )
    if missing:
        result.status = PERSON_MISSING_SOURCE
        result.detail = "unresolved MISSING_SOURCE; operator phải retire hoặc khôi phục bytes"
        return result
    if not pre.ok:
        result.detail = pre.summary_text()
        return result
    if not inventory.sources:
        if retired:
            result.status = PERSON_ALREADY_COMPLETE
            result.detail = "only retired source history remains"
        else:
            result.detail = "thư mục hồ sơ không có PDF active"
        return result
    relevant_legacy = [item for item in (legacy.outcomes if legacy else []) if item.source_file in legacy_candidates]
    if legacy_untracked_missing or any(item.outcome == OUTCOME_MISSING_LEGACY_SOURCE for item in relevant_legacy):
        result.status = PERSON_MISSING_SOURCE
        result.detail = "legacy ledger has source bytes absent from input; operator policy required"
        return result
    if legacy and any(
        item.outcome in (OUTCOME_REVIEW_REQUIRED, OUTCOME_NOT_LEGACY_SOURCE)
        for item in relevant_legacy
    ):
        result.status = PERSON_NEEDS_REVIEW
        result.detail = "legacy ledger không đủ bằng chứng để hydrate tự động"
        return result

    # The Antigravity agent writes analysis JSON itself.  Check its local
    # contract before invoking the pipeline so a planning invocation never
    # changes a NEW source into FAILED merely because the agent has not read it
    # yet.  Fixture/custom providers keep their existing direct behavior.
    analysis_path = getattr(provider, "analysis_path", None)
    if callable(analysis_path):
        required = [
            source.name for source in scan.needs_agent_sources
            if not Path(analysis_path(source.path)).is_file()
        ]
        if required:
            result.vision_required_sources = required
            result.detail = "ANALYSIS_REQUIRED (NEW/STALE only): " + ", ".join(required)
            return result

    dry = process_person_folder(
        folder, mode=MODE_DRY_RUN, provider=provider, workspace=ws,
        state_registry=registry,
    )
    result.vision_read_sources = len(dry.incremental.needs_agent_sources) if dry.incremental else 0
    result.review = dry.manifest.get("summary", {}).get("review_pending", 0)
    result.missing = dry.manifest.get("summary", {}).get("missing_sources", 0)
    result.retired = dry.manifest.get("summary", {}).get("retired_sources", retired)
    if dry.status.startswith("BLOCKED") or dry.manifest.get("failed_this_run"):
        result.detail = dry.status if not dry.manifest.get("failed_this_run") else str(dry.manifest["failed_this_run"])
        return result
    if result.missing:
        result.status = PERSON_MISSING_SOURCE
        result.detail = "unresolved MISSING_SOURCE discovered during dry-run"
        return result
    if result.review:
        result.status = PERSON_NEEDS_REVIEW
        result.detail = "REVIEW_PENDING; automatic apply is forbidden"
        return result
    naming_reasons = _naming_reasons(registry, person, present_hashes)
    if naming_reasons:
        result.status = PERSON_NEEDS_REVIEW
        result.detail = "naming deterministic preflight failed: " + "; ".join(naming_reasons)
        return result

    no_work = result.new_pdfs == 0 and counts[DECISION_CACHED_PENDING_APPLY] == 0
    if not apply_enabled:
        result.status = PERSON_ALREADY_COMPLETE if no_work else PERSON_READY
        result.detail = "validated; --no-apply/dry-run selected"
        return result
    apply = process_person_folder(
        folder, mode=MODE_APPLY, provider=provider, workspace=ws,
        state_registry=registry,
    )
    result.applied = True
    result.outputs = _output_count(ws, person)
    post = _reconcile(ws, registry, person, folder)
    result.reconciliation_ok = post.ok
    post_summary = apply.manifest.get("summary", {})
    result.review = post_summary.get("review_pending", result.review)
    result.missing = post_summary.get("missing_sources", result.missing)
    if apply.status.startswith("BLOCKED") or apply.manifest.get("failed_this_run") or not post.ok:
        result.status = PERSON_BLOCKED
        result.detail = apply.status if post.ok else post.summary_text()
        return result
    result.status = PERSON_ALREADY_COMPLETE if no_work else PERSON_COMPLETED
    return result


def run_batch(
    input_dir: Path,
    *,
    workspace: Optional[Workspace] = None,
    provider: Optional[DocumentVisionProvider] = None,
    provider_name: str = "agent",
    provider_config: Optional[dict] = None,
    apply_enabled: bool = True,
    person: Optional[str] = None,
    write_report: bool = True,
) -> BatchReport:
    """Process all known dossiers, isolating every person-level failure."""
    input_dir = Path(input_dir)
    ws = workspace or Workspace.discover(input_dir)
    if not input_dir.is_dir():
        raise PipelineError(f"Không tìm thấy input directory: {input_dir}")
    # Global preflight happens before any state or output mutation.
    load_catalog()
    current_fingerprint(load_catalog())
    git = _git_integrity(ws.root)
    runtime_provider = provider or get_provider(provider_name, provider_config)
    report = BatchReport(
        input_dir=str(input_dir), apply_enabled=apply_enabled,
        provider=runtime_provider.describe(), git_integrity=git,
    )
    if git["status"] == "DIRTY":
        report.global_error = "baseline Git working tree is dirty"
        if write_report:
            report.report_path = save_manifest(report.as_dict(), ws.logs / "batch-report.json")
        return report
    try:
        with StateRegistry(ws.state_db_path) as registry:
            for name in _folder_names(input_dir, registry, person):
                folder = input_dir / name
                try:
                    report.people.append(_person_result(
                        folder, ws=ws, registry=registry, provider=runtime_provider,
                        apply_enabled=apply_enabled,
                    ))
                except Exception as exc:
                    # SQLite corruption is global; a broken PDF/analysis/file
                    # system error is confined to this person and the batch continues.
                    import sqlite3
                    if isinstance(exc, sqlite3.DatabaseError):
                        report.global_error = f"state DB error while processing {name}: {exc}"
                        break
                    report.people.append(BatchPersonResult(
                        person_folder=name, status=PERSON_BLOCKED,
                        detail=f"{type(exc).__name__}: {exc}",
                    ))
    except Exception as exc:
        report.global_error = f"state DB initialization failed: {type(exc).__name__}: {exc}"
    report.people.sort(key=lambda item: item.person_folder.casefold())
    if write_report:
        report.report_path = save_manifest(report.as_dict(), ws.logs / "batch-report.json")
    return report
