"""Recovery/import canonical state từ legacy manifest và managed artifacts.

Các bản apply trước khi ``logical_documents`` trở thành canonical từng chỉ được
import ở cấp source.  Module này hydrate lại từng logical document từ ledger
đã có, nhưng chỉ khi identity SHA/page-count, analysis page grouping và managed
artifact đều khớp.  Không đọc lại PDF bằng provider, không tạo PDF và không tự
resolve REVIEW.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .agent_contract import AnalysisContractError, load_analysis
from .catalog import load_catalog
from .fingerprint import current_fingerprint
from .manifest import load_manifest
from .models import PipelineError
from .pdf_inventory import PersonInventory, build_inventory, list_pdfs
from .pipeline import Workspace
from .policy import CLASSIFICATION_KIND_TAXONOMY
from .state import (
    RESOLUTION_AUTO_RESOLVED,
    RESOLUTION_REVIEW_PENDING,
    StateRegistry,
)

OUTCOME_IMPORTED = "IMPORTED"
OUTCOME_ALREADY_IN_REGISTRY = "ALREADY_IN_REGISTRY"
OUTCOME_REVIEW_REQUIRED = "STATE_IMPORT_REVIEW_REQUIRED"

OUTCOME_RECOVERED = "RECOVERED"
OUTCOME_ALREADY_HYDRATED = "ALREADY_HYDRATED"
OUTCOME_NOT_LEGACY_SOURCE = "NO_LEGACY_LEDGER"
OUTCOME_MISSING_LEGACY_SOURCE = "MISSING_LEGACY_SOURCE_PENDING_OPERATOR_POLICY"


@dataclass
class ImportOutcome:
    source_file: str
    outcome: str
    detail: str = ""


@dataclass
class ImportReport:
    person_folder: str
    outcomes: list[ImportOutcome] = field(default_factory=list)

    def summary_text(self) -> str:
        lines = [f"IMPORT STATE: {self.person_folder}", ""]
        for o in self.outcomes:
            lines.append(f"  [{o.outcome}] {o.source_file}" + (f" - {o.detail}" if o.detail else ""))
        return "\n".join(lines)


@dataclass
class LegacyRecoveryOutcome:
    source_file: str
    outcome: str
    restored_logical_documents: int = 0
    detail: str = ""


@dataclass
class LegacyRecoveryReport:
    person_folder: str
    outcomes: list[LegacyRecoveryOutcome] = field(default_factory=list)

    def summary_text(self) -> str:
        lines = [f"RECOVER LEGACY STATE: {self.person_folder}", ""]
        for o in self.outcomes:
            count = f" ({o.restored_logical_documents} logical document)" if o.restored_logical_documents else ""
            lines.append(f"  [{o.outcome}] {o.source_file}{count}" + (f" - {o.detail}" if o.detail else ""))
        return "\n".join(lines)


def import_person_folder(
    folder: Path, registry: StateRegistry, *, workspace: Optional[Workspace] = None
) -> ImportReport:
    recovered = recover_legacy_person_folder(folder, registry, workspace=workspace)
    report = ImportReport(person_folder=recovered.person_folder)
    for outcome in recovered.outcomes:
        if outcome.outcome == OUTCOME_RECOVERED:
            report.outcomes.append(
                ImportOutcome(outcome.source_file, OUTCOME_IMPORTED, outcome.detail)
            )
        elif outcome.outcome == OUTCOME_ALREADY_HYDRATED:
            report.outcomes.append(
                ImportOutcome(outcome.source_file, OUTCOME_ALREADY_IN_REGISTRY, outcome.detail)
            )
        elif outcome.outcome == OUTCOME_NOT_LEGACY_SOURCE:
            report.outcomes.append(
                ImportOutcome(outcome.source_file, OUTCOME_REVIEW_REQUIRED, outcome.detail)
            )
        else:
            report.outcomes.append(
                ImportOutcome(outcome.source_file, OUTCOME_REVIEW_REQUIRED, outcome.detail)
            )
    return report


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise PipelineError(f"Legacy manifest có {field} không phải chuỗi/mảng chuỗi.")


def _legacy_documents(
    entries: list[dict], *, source_file: str, output_dir: Path, review_dir: Path, analysis_documents: dict
) -> list[dict]:
    """Chuyển legacy ledger entries thành schema hydrate, không suy đoán."""
    documents: list[dict] = []
    for entry in entries:
        if entry.get("source_file") != source_file:
            raise PipelineError("Legacy manifest grouping source_file không nhất quán.")
        status = entry.get("status")
        if status not in ("AUTO", "REVIEW"):
            raise PipelineError("Legacy manifest thiếu status AUTO/REVIEW để khôi phục semantics.")
        pages = entry.get("source_pages")
        agent_document = analysis_documents.get(tuple(pages)) if isinstance(pages, list) else None
        if agent_document is None:
            raise PipelineError("analysis không có logical document cùng source_pages với ledger.")
        classification_status = entry.get("classification_status")
        if classification_status is None:
            # Manifest incremental hiện hành lưu final resolution; metadata
            # phân loại gốc lấy từ analysis đã frozen, không từ filename.
            classification_status = "REVIEW" if agent_document.needs_review else "AUTO"
        if classification_status not in ("AUTO", "REVIEW"):
            raise PipelineError("Legacy manifest có classification_status không hợp lệ.")

        target_dir = entry.get("target_dir")
        target_file = entry.get("target_file") or entry.get("current_target_filename")
        if target_dir not in ("output", "review") or not isinstance(target_file, str) or not target_file:
            raise PipelineError("Legacy manifest thiếu target_dir/target_file hợp lệ.")
        target_name = Path(target_file)
        if target_name.name != target_file:
            raise PipelineError("Legacy manifest target_file không được chứa path.")
        artifact = (output_dir if target_dir == "output" else review_dir) / target_name
        if not artifact.is_file():
            raise PipelineError(f"thiếu managed artifact: {target_file}")

        try:
            confidence = float(entry.get("confidence") if entry.get("confidence") is not None else agent_document.confidence)
            date_confidence = float(
                entry.get("date_confidence") if entry.get("date_confidence") is not None else agent_document.date_confidence
            )
        except (TypeError, ValueError) as exc:
            raise PipelineError("Legacy manifest có confidence không hợp lệ.") from exc
        if not (0.0 <= confidence <= 1.0 and 0.0 <= date_confidence <= 1.0):
            raise PipelineError("Legacy manifest có confidence ngoài khoảng 0..1.")

        kind = entry.get("classification_kind") or CLASSIFICATION_KIND_TAXONOMY
        documents.append(
            {
                "logical_document_id": entry.get("logical_document_id"),
                "source_pages": pages,
                "type_id": entry.get("type_id") or "UNKNOWN",
                "confidence": confidence,
                "document_date": entry.get("document_date") if entry.get("document_date") is not None else agent_document.document_date,
                "date_confidence": date_confidence,
                "title_short": entry.get("title_short") or agent_document.title_short,
                "segmentation_flags": _string_list(entry.get("segmentation_flags"), "segmentation_flags"),
                "classification_status": classification_status,
                "classification_reasons": _string_list(entry.get("review_reason"), "review_reason"),
                # Legacy ``status`` là trạng thái cuối; REVIEW có artifact review
                # nhưng không có bằng chứng operator resolve, nên vẫn pending.
                "resolution_status": (
                    RESOLUTION_REVIEW_PENDING if status == "REVIEW" else RESOLUTION_AUTO_RESOLVED
                ),
                "current_target_filename": target_file,
                "target_dir": target_dir,
                "sequence_index": entry.get("sequence"),
                "classification_kind": kind,
                "subtype": entry.get("subtype"),
                "date_precision": entry.get("date_precision"),
                "duplicate_of": entry.get("duplicate_of"),
                "resolved_classification_kind": None,
                "resolved_type_id": None,
                "resolved_subtype": None,
                "resolved_document_date": None,
                "resolved_date_precision": None,
                "resolved_by": None,
                "resolved_at": None,
            }
        )
    return documents


def _load_analysis(analysis_path: Path, *, source_file: str, page_count: int):
    """Chỉ validate JSON đã có; không gọi provider/Vision."""
    catalog = load_catalog()
    return load_analysis(analysis_path, catalog, expect_source=source_file, expect_pages=page_count)


def _validate_analysis_grouping(analysis, legacy_documents: list[dict]) -> None:
    from_analysis = sorted(tuple(doc.source_pages) for doc in analysis.documents)
    from_ledger = sorted(tuple(doc["source_pages"]) for doc in legacy_documents)
    if from_analysis != from_ledger:
        raise PipelineError("analysis page grouping không khớp legacy manifest.")


def recover_legacy_person_folder(
    folder: Path, registry: StateRegistry, *, workspace: Optional[Workspace] = None
) -> LegacyRecoveryReport:
    """Hydrate state reusable, source-scoped, deterministic và idempotent.

    Chỉ source đang hiện diện trong ``folder`` được xem xét để hydrate.  Ledger
    source đã mất byte được báo riêng, tuyệt đối không tự tạo/alias/xoá.
    """
    folder = Path(folder)
    ws = workspace or Workspace.discover(folder)
    # Empty input is meaningful for recovery: it can mean every legacy source
    # was removed and must be quarantined/reported, not hydrated by filename.
    inventory: PersonInventory
    if list_pdfs(folder):
        inventory = build_inventory(folder)
    else:
        inventory = PersonInventory(person_folder=folder.name, folder=folder, sources=[])
    output_dir = ws.output / inventory.person_folder
    review_dir = ws.review / inventory.person_folder
    ledger_path = output_dir / "_manifest.json"
    ledger = load_manifest(ledger_path)
    report = LegacyRecoveryReport(person_folder=inventory.person_folder)

    if ledger is None:
        for src in inventory.sources:
            report.outcomes.append(
                LegacyRecoveryOutcome(src.name, OUTCOME_REVIEW_REQUIRED, detail=f"chưa có ledger {ledger_path}")
            )
        return report

    ledger_sources = {s.get("file"): s for s in ledger.get("sources", []) if isinstance(s, dict)}
    docs_by_source: dict[str, list[dict]] = {}
    for entry in ledger.get("documents", []):
        if isinstance(entry, dict) and isinstance(entry.get("source_file"), str):
            docs_by_source.setdefault(entry["source_file"], []).append(entry)

    fingerprint = current_fingerprint(load_catalog())
    inventory_names = {src.name for src in inventory.sources}
    for src in inventory.sources:
        ledger_source = ledger_sources.get(src.name)
        entries = docs_by_source.get(src.name)
        if ledger_source is None or not entries:
            report.outcomes.append(
                LegacyRecoveryOutcome(
                    src.name, OUTCOME_NOT_LEGACY_SOURCE,
                    detail="không có evidence legacy đầy đủ trong ledger; không thay đổi state",
                )
            )
            continue
        if ledger_source.get("sha256") != src.sha256:
            report.outcomes.append(
                LegacyRecoveryOutcome(src.name, OUTCOME_REVIEW_REQUIRED, detail="SHA-256 ledger không khớp PDF hiện diện")
            )
            continue
        if ledger_source.get("pages") != src.pages:
            report.outcomes.append(
                LegacyRecoveryOutcome(src.name, OUTCOME_REVIEW_REQUIRED, detail="page count ledger không khớp PDF hiện diện")
            )
            continue

        try:
            analysis_path = ws.root / "analysis" / inventory.person_folder / f"{src.path.stem}.json"
            analysis = _load_analysis(analysis_path, source_file=src.name, page_count=src.pages)
            docs = _legacy_documents(
                entries, source_file=src.name, output_dir=output_dir, review_dir=review_dir,
                analysis_documents={tuple(doc.source_pages): doc for doc in analysis.documents},
            )
            _validate_analysis_grouping(analysis, docs)
            result = registry.hydrate_legacy_logical_documents(
                src.sha256,
                source_filename=src.name,
                source_relative_path=f"{inventory.person_folder}/{src.name}",
                person_folder=inventory.person_folder,
                page_count=src.pages,
                documents=docs,
                manifest_path=str(ledger_path),
                taxonomy_version=fingerprint.taxonomy_version,
                analysis_schema_version=fingerprint.analysis_schema_version,
            )
        except (PipelineError, AnalysisContractError) as exc:
            report.outcomes.append(
                LegacyRecoveryOutcome(src.name, OUTCOME_REVIEW_REQUIRED, detail=str(exc))
            )
            continue

        if result.restored_logical_document_ids:
            report.outcomes.append(
                LegacyRecoveryOutcome(
                    src.name, OUTCOME_RECOVERED,
                    restored_logical_documents=len(result.restored_logical_document_ids),
                    detail=f"status canonical -> {result.source_status}",
                )
            )
        else:
            report.outcomes.append(
                LegacyRecoveryOutcome(
                    src.name, OUTCOME_ALREADY_HYDRATED,
                    detail=f"status canonical = {result.source_status}",
                )
            )

    for source_name, ledger_source in sorted(ledger_sources.items()):
        if source_name not in inventory_names:
            report.outcomes.append(
                LegacyRecoveryOutcome(
                    str(source_name), OUTCOME_MISSING_LEGACY_SOURCE,
                    detail="có ledger nhưng source bytes không còn trong input; không hydrate/alias/xoá",
                )
            )
    return report
