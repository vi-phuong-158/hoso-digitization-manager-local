"""Điều phối Phase A -> G cho một thư mục hồ sơ (một người).

Mặc định dry-run. Apply chỉ chạy khi QC đạt.

Có hai đường đi:
  - `state_registry=None` (mặc định của hàm): xử lý TOÀN BỘ nguồn trong thư
    mục mỗi lần gọi, y hệt hành vi gốc trước khi có incremental processing.
    Toàn bộ test cũ và `golden.py` dùng đường đi này, không đổi hành vi.
  - `state_registry=<StateRegistry>`: đường đi incremental + global cross-run
    naming. Trước khi đọc bất kỳ PDF nào, đối chiếu SHA-256 + fingerprint
    (taxonomy/schema) với registry; chỉ nguồn NEW/STALE (hoặc retry rõ ràng)
    mới được đưa cho provider đọc. Đặt tên `.1/.2/...` nhìn TOÀN BỘ logical
    document đã biết của một người (không chỉ lượt chạy hiện tại) - xem
    `app/global_naming.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pypdf import PdfReader

from .catalog import Catalog, find_catalog_path, load_catalog
from .classifier import DEFAULT_POLICY, ConfidencePolicy, classify_document
from .fingerprint import current_fingerprint
from .global_naming import (
    NameableDoc,
    SAME_DATE_TIE_BREAK,
    build_rename_plan,
    compute_global_assignment,
    compute_supporting_assignment,
    execute_rename_plan,
    has_collisions,
)
from .incremental import IncrementalScan, scan_person_folder
from .manifest import (
    build_manifest,
    document_entry_sort_key,
    load_manifest,
    save_manifest,
    source_entry_sort_key,
)
from .models import MODE_APPLY, MODE_DRY_RUN, ClassifiedDocument, PipelineError
from .naming import DEFAULT_NAMING_POLICY, NamingPolicy, assign_names, review_filename
from .policy import (
    CLASSIFICATION_KIND_DUPLICATE,
    CLASSIFICATION_KIND_SUPPORTING,
    CLASSIFICATION_KIND_TAXONOMY,
    supporting_group_key,
)
from .pdf_inventory import PersonInventory, SourceFile, build_inventory, verify_unchanged
from .qc import QCReport, run_qc
from .segmenter import (
    DEFAULT_SEGMENTATION_CONFIG,
    SegmentationConfig,
    segment_source,
)
from .state import DB_FILENAME, SOURCE_MISSING, LogicalDocumentRow, StateRegistry
from .vision_adapter import DocumentVisionProvider, get_provider, validate_page_observation
from .writer import LEDGER_FILENAME, WriteResult, apply_documents, plan_targets, split_pages, verify_outputs

# Re-export để tương thích ngược: `from app.pipeline import MODE_APPLY, MODE_DRY_RUN`.
__all__ = [
    "MODE_APPLY",
    "MODE_DRY_RUN",
    "Workspace",
    "PipelineResult",
    "process_person_folder",
]


@dataclass
class Workspace:
    root: Path

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def review(self) -> Path:
        return self.root / "review"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def state_db_path(self) -> Path:
        return self.state_dir / DB_FILENAME

    @classmethod
    def discover(cls, start: Optional[Path] = None) -> "Workspace":
        return cls(find_catalog_path(start).parent)


@dataclass
class PipelineResult:
    person_folder: str
    mode: str
    inventory: PersonInventory
    documents: list[ClassifiedDocument]
    qc: QCReport
    manifest: dict
    manifest_path: Optional[Path] = None
    write_result: Optional[WriteResult] = None
    notes: list[str] = field(default_factory=list)
    incremental: Optional[IncrementalScan] = None
    status_override: Optional[str] = None

    @property
    def status(self) -> str:
        """Trạng thái kết thúc theo RUNBOOK_ANTIGRAVITY.md (đúng 5 giá trị)."""
        if self.status_override is not None:
            return self.status_override
        if not self.qc.passed:
            return "BLOCKED_QC"
        if self.mode == MODE_APPLY:
            if self.write_result is not None and not self.write_result.ok:
                return "BLOCKED_RUNTIME"
            return "APPLY_PASS"
        if any(d.final_status == "REVIEW" for d in self.documents):
            return "REVIEW_REQUIRED"
        return "DRY_RUN_PASS"


def process_person_folder(
    folder: Path,
    *,
    mode: str = MODE_DRY_RUN,
    provider: Optional[DocumentVisionProvider] = None,
    provider_name: str = "fixture",
    provider_config: Optional[dict] = None,
    workspace: Optional[Workspace] = None,
    catalog: Optional[Catalog] = None,
    segmentation_config: SegmentationConfig = DEFAULT_SEGMENTATION_CONFIG,
    confidence_policy: ConfidencePolicy = DEFAULT_POLICY,
    naming_policy: NamingPolicy = DEFAULT_NAMING_POLICY,
    force: bool = False,
    write_manifest: bool = True,
    state_registry: Optional[StateRegistry] = None,
    retry_review: bool = False,
    retry_failed: bool = False,
) -> PipelineResult:
    if mode not in (MODE_DRY_RUN, MODE_APPLY):
        raise PipelineError(f"mode không hợp lệ: {mode!r}")

    folder = Path(folder)
    ws = workspace or Workspace.discover(folder)
    catalog = catalog or load_catalog()
    provider = provider or get_provider(provider_name, provider_config)

    # Phase A - Inventory (chỉ đọc).
    inventory = build_inventory(folder)

    output_dir = ws.output / inventory.person_folder
    review_dir = ws.review / inventory.person_folder

    if state_registry is not None:
        return _process_incremental(
            inventory=inventory, mode=mode, provider=provider, catalog=catalog, ws=ws,
            output_dir=output_dir, review_dir=review_dir, segmentation_config=segmentation_config,
            confidence_policy=confidence_policy, naming_policy=naming_policy, write_manifest=write_manifest,
            state_registry=state_registry, retry_review=retry_review, retry_failed=retry_failed,
        )

    # ================= Đường đi CŨ (không state-aware) =================
    # Xử lý TOÀN BỘ nguồn mỗi lần gọi - hành vi giữ nguyên 100% so với trước
    # khi có incremental processing. golden.py và mọi test cũ dùng nhánh này.
    documents: list[ClassifiedDocument] = []
    notes: list[str] = []
    for src in inventory.sources:
        documents.extend(
            _process_source(provider, catalog, src, segmentation_config, confidence_policy, notes)
        )

    assign_names(catalog, documents, output_dir, review_dir, naming_policy)

    write_result: Optional[WriteResult] = None
    output_problems: Optional[list[str]] = None
    targets = plan_targets(documents, inventory, output_dir, review_dir)
    targets_by_name = {v["target_file"]: v for v in targets.values()}

    if mode == MODE_APPLY:
        pre_qc = run_qc(catalog, inventory, documents, output_dir, review_dir)
        if not pre_qc.passed:
            manifest = build_manifest(
                catalog, inventory, documents, pre_qc, mode=mode,
                provider=provider.describe(), targets=targets_by_name,
            )
            path = _persist(manifest, ws, inventory.person_folder, mode) if write_manifest else None
            return PipelineResult(
                person_folder=inventory.person_folder, mode=mode, inventory=inventory,
                documents=documents, qc=pre_qc, manifest=manifest, manifest_path=path,
                notes=["QC không đạt -> không apply."],
            )
        previous = load_manifest(output_dir / LEDGER_FILENAME)
        write_result = apply_documents(
            documents, inventory, output_dir, review_dir, previous_ledger=previous, force=force
        )
        if write_result.ok:
            output_problems = verify_outputs(documents, output_dir, review_dir)
        else:
            notes.append("Có xung đột file đầu ra -> không ghi gì cả (fail-safe).")

    qc = run_qc(catalog, inventory, documents, output_dir, review_dir, output_problems)

    if write_result is not None:
        for name, sha in write_result.output_sha256.items():
            if name in targets_by_name:
                targets_by_name[name]["output_sha256"] = sha

    manifest = build_manifest(
        catalog, inventory, documents, qc, mode=mode,
        provider=provider.describe(), targets=targets_by_name,
    )
    if write_result is not None:
        manifest["write_result"] = write_result.as_dict()

    manifest_path = None
    if write_manifest:
        manifest_path = _persist(manifest, ws, inventory.person_folder, mode)
        if mode == MODE_APPLY and write_result is not None and write_result.ok:
            save_manifest(manifest, output_dir / LEDGER_FILENAME)

    return PipelineResult(
        person_folder=inventory.person_folder, mode=mode, inventory=inventory,
        documents=documents, qc=qc, manifest=manifest, manifest_path=manifest_path,
        write_result=write_result, notes=notes,
    )


def _process_source(
    provider: DocumentVisionProvider,
    catalog: Catalog,
    src: SourceFile,
    segmentation_config: SegmentationConfig,
    confidence_policy: ConfidencePolicy,
    notes: list[str],
) -> list[ClassifiedDocument]:
    """Phase B->D cho MỘT file nguồn. Dùng chung bởi cả hai đường đi."""
    page_numbers = list(range(1, src.pages + 1))
    observations = provider.analyze_pages(src.path, page_numbers)
    for obs in observations:
        validate_page_observation(obs, catalog, where=f"{src.name}/trang {obs.page_number}")
    by_page = {o.page_number: o for o in observations}

    logical_docs = segment_source(src, observations, segmentation_config)

    proposed = provider.proposed_documents(src.path)
    if proposed is not None:
        mismatches = cross_check_segmentation(logical_docs, proposed)
        if mismatches:
            notes.append(
                f"{src.name}: segmenter local và đề xuất của Agent lệch nhau ở "
                f"{len(mismatches)} nhóm trang -> đưa REVIEW."
            )

    out: list[ClassifiedDocument] = []
    for ldoc in logical_docs:
        doc_obs = [by_page[p] for p in ldoc.source_pages]
        out.append(
            classify_document(provider, catalog, src.path, ldoc, doc_obs, policy=confidence_policy)
        )
    return out


def _analysis_qc(
    inventory: PersonInventory, documents: list[ClassifiedDocument], sources: list[SourceFile]
) -> QCReport:
    """QC ngay sau Phase B-D, TRƯỚC khi đặt tên (naming toàn cục chạy sau, ở
    apply). Chỉ kiểm coverage/overlap/status - không kiểm tên file vì chưa có."""
    report = QCReport()
    coverage_problems: list[str] = []
    overlap_problems: list[str] = []
    for src in sources:
        seen: dict[int, str] = {}
        for doc in documents:
            if doc.document.source_file != src.name:
                continue
            for p in doc.document.source_pages:
                if p in seen:
                    overlap_problems.append(f"{src.name} trang {p}: {seen[p]} & {doc.document.doc_key}")
                seen[p] = doc.document.doc_key
        missing = sorted(set(range(1, src.pages + 1)) - set(seen))
        if missing:
            coverage_problems.append(f"{src.name}: thiếu trang {missing}")
    total_pages = sum(s.pages for s in sources)
    covered = sum(len(d.document.source_pages) for d in documents)
    report.add(
        "page_coverage", not coverage_problems and covered == total_pages,
        f"{covered}/{total_pages} trang được gán tài liệu. " + "; ".join(coverage_problems),
    )
    report.add("page_overlap", not overlap_problems, "; ".join(overlap_problems) or "0 trang bị dùng lặp")
    bad_status = [d.document.doc_key for d in documents if d.classification_status not in ("AUTO", "REVIEW")]
    report.add(
        "status_coverage", not bad_status and len(documents) > 0,
        "; ".join(bad_status) or f"{len(documents)} logical document đều có trạng thái",
    )
    sane = 1 <= len(documents) <= total_pages if total_pages else len(documents) == 0
    report.add("document_count_sane", sane, f"{len(documents)} logical document / {total_pages} trang nguồn")
    return report


def _to_cache_dict(cd: ClassifiedDocument) -> dict:
    return {
        "source_pages": list(cd.document.source_pages),
        "type_id": cd.classification.type_id,
        "confidence": cd.classification.confidence,
        "document_date": cd.classification.document_date,
        "date_confidence": cd.classification.date_confidence,
        "title_short": cd.classification.title_short,
        "segmentation_flags": list(cd.document.segmentation_flags),
        "classification_status": cd.classification_status,
        "classification_reasons": list(cd.classification_reasons),
        # DEV POLICY CLOSURE: phân tích lần đầu luôn là TAXONOMY thuần (Agent
        # không được tự gán SUPPORTING/DUPLICATE - chỉ resolve-review mới có
        # quyền đó). date_precision để None -> save_analysis tự suy DAY nếu có
        # document_date, không suy MONTH/YEAR ở đây (không tự bịa từ Vision).
        "classification_kind": CLASSIFICATION_KIND_TAXONOMY,
        "subtype": None,
        "date_precision": None,
    }


# ===========================================================================
# Đường đi incremental + global cross-run naming
# ===========================================================================
def _process_incremental(
    *,
    inventory: PersonInventory,
    mode: str,
    provider: DocumentVisionProvider,
    catalog: Catalog,
    ws: Workspace,
    output_dir: Path,
    review_dir: Path,
    segmentation_config: SegmentationConfig,
    confidence_policy: ConfidencePolicy,
    naming_policy: NamingPolicy,
    write_manifest: bool,
    state_registry: StateRegistry,
    retry_review: bool,
    retry_failed: bool,
) -> PipelineResult:
    present_hashes = {source.sha256 for source in inventory.sources}
    missing_sources = [
        item for item in state_registry.source_lifecycle(inventory.person_folder, present_hashes)
        if item.lifecycle_status == SOURCE_MISSING
    ]
    if missing_sources and mode == MODE_APPLY:
        # An unresolved historical source is a reportable safety blocker for
        # APPLY.  Explicit `retire-source` is the only transition that clears
        # that blocker; dry-run may still inspect current sources.
        scan = scan_person_folder(
            inventory, state_registry, mode=MODE_DRY_RUN, fingerprint=current_fingerprint(catalog),
            retry_review=retry_review, retry_failed=retry_failed,
            output_dir=output_dir, review_dir=review_dir,
        )
        qc = QCReport()
        notes = [
            "BLOCKED_MISSING_SOURCE: " + "; ".join(
                f"{item.source_filename} (sha256 {item.source_hash})" for item in missing_sources
            )
        ]
        manifest = _incremental_manifest(
            catalog, inventory, provider, scan, qc, mode=mode, failed={}, notes=notes,
            state_registry=state_registry,
        )
        manifest_path = _persist(manifest, ws, inventory.person_folder, mode) if write_manifest else None
        return PipelineResult(
            person_folder=inventory.person_folder, mode=mode, inventory=inventory,
            documents=[], qc=qc, manifest=manifest, manifest_path=manifest_path,
            notes=notes, incremental=scan, status_override="BLOCKED_MISSING_SOURCE",
        )

    fp = current_fingerprint(catalog)
    scan = scan_person_folder(
        inventory, state_registry, mode=mode, fingerprint=fp,
        retry_review=retry_review, retry_failed=retry_failed,
        output_dir=output_dir, review_dir=review_dir,
    )

    notes: list[str] = []
    failed: dict[str, str] = {}  # source_hash -> lỗi

    agent_decisions = [d for d in scan.decisions if d.needs_agent]
    for d in agent_decisions:
        state_registry.begin_processing(
            source_hash=d.source.sha256, source_filename=d.source.name,
            source_relative_path=f"{inventory.person_folder}/{d.source.name}",
            person_folder=inventory.person_folder, page_count=d.source.pages,
        )

    fresh_by_source: dict[str, list[ClassifiedDocument]] = {}
    for d in agent_decisions:
        src = d.source
        try:
            docs = _process_source(provider, catalog, src, segmentation_config, confidence_policy, notes)
            fresh_by_source[src.sha256] = docs
            state_registry.save_analysis(
                src.sha256,
                documents=[_to_cache_dict(cd) for cd in docs],
                taxonomy_version=fp.taxonomy_version,
                analysis_schema_version=fp.analysis_schema_version,
            )
        except Exception as exc:  # cô lập lỗi theo từng nguồn - 1 file hỏng không sập cả lượt
            failed[src.sha256] = str(exc)
            notes.append(f"{src.name}: LỖI xử lý -> {exc}")
            state_registry.mark_failed(src.sha256, error=str(exc))

    # QC coverage/overlap chỉ áp cho các nguồn VỪA được Agent đọc lượt này -
    # nguồn dùng lại cache đã qua QC này ở đúng lượt nó được phân tích lần đầu.
    fresh_ok_sources = [d.source for d in agent_decisions if d.source.sha256 not in failed]
    qc = QCReport()
    if fresh_ok_sources:
        fresh_documents = [cd for s in fresh_ok_sources for cd in fresh_by_source[s.sha256]]
        qc = _analysis_qc(inventory, fresh_documents, fresh_ok_sources)
        if not qc.passed:
            for s in fresh_ok_sources:
                if s.sha256 in failed:
                    continue
                state_registry.mark_failed(
                    s.sha256,
                    error="QC (phân tích) không đạt: " + "; ".join(f"{c.name}: {c.detail}" for c in qc.failures),
                )
                failed[s.sha256] = "QC phân tích không đạt"

    apply_sources = [d.source for d in scan.decisions if d.needs_apply and d.source.sha256 not in failed]

    if mode == MODE_DRY_RUN:
        return _finish_dry_run(
            inventory=inventory, ws=ws, catalog=catalog, provider=provider, scan=scan, qc=qc,
            notes=notes, failed=failed, write_manifest=write_manifest, state_registry=state_registry,
        )

    return _finish_apply(
        inventory=inventory, ws=ws, output_dir=output_dir, review_dir=review_dir, catalog=catalog,
        provider=provider, scan=scan, apply_sources=apply_sources, notes=notes, failed=failed,
        qc=qc, naming_policy=naming_policy, state_registry=state_registry, write_manifest=write_manifest,
    )


def _name_of(scan: IncrementalScan, source_hash: str) -> str:
    for d in scan.decisions:
        if d.source.sha256 == source_hash:
            return d.source.name
    return source_hash[:12]


def _failed_report(scan: IncrementalScan, failed: dict[str, str]) -> list[dict]:
    return [{"source_file": _name_of(scan, h), "error": e} for h, e in failed.items()]


def _incremental_manifest(
    catalog: Catalog,
    inventory: PersonInventory,
    provider: DocumentVisionProvider,
    scan: IncrementalScan,
    qc: QCReport,
    *,
    mode: str,
    failed: dict[str, str],
    notes: list[str],
    state_registry: StateRegistry,
) -> dict:
    docs = []
    for row in state_registry.logical_documents_for_person(inventory.person_folder):
        src_state = state_registry.get(row.source_hash)
        kind = row.effective_classification_kind
        is_taxonomy = kind == CLASSIFICATION_KIND_TAXONOMY
        docs.append(
            {
                "logical_document_id": row.logical_document_id,
                "source_hash": row.source_hash,
                "source_file": src_state.source_filename if src_state else None,
                "source_pages": list(row.source_pages),
                "classification_kind": kind,
                "type_id": row.effective_type_id if is_taxonomy else None,
                "type_name_vi": catalog.name_vi(row.effective_type_id) if is_taxonomy and catalog.is_valid_classification(row.effective_type_id) and row.effective_type_id != "UNKNOWN" else None,
                "subtype": row.effective_subtype,
                "duplicate_of": row.duplicate_of,
                "document_date": row.effective_document_date,
                "date_precision": row.effective_date_precision,
                "title_short": row.title_short,
                "resolution_status": row.resolution_status,
                "current_target_filename": row.current_target_filename,
                # Alias tương thích ngược với app/state_import.py (đọc ledger cũ).
                "target_file": row.current_target_filename,
                "target_dir": row.target_dir,
                "sequence_index": row.sequence_index,
                "needs_review": row.resolution_status == "REVIEW_PENDING",
                "review_reason": (row.classification_reasons or None) if row.resolution_status == "REVIEW_PENDING" else None,
                "status": "REVIEW" if row.resolution_status == "REVIEW_PENDING" else "AUTO",
            }
        )
    sources_list = [
        {"file": s.source_filename, "sha256": s.source_hash, "pages": s.page_count}
        for s in state_registry.all(inventory.person_folder)
    ]
    docs.sort(key=document_entry_sort_key)
    sources_list.sort(key=source_entry_sort_key)
    canonical_summary = state_registry.summarize_person(
        inventory.person_folder, {source.sha256 for source in inventory.sources}
    )
    source_lifecycle = state_registry.source_lifecycle(
        inventory.person_folder, {source.sha256 for source in inventory.sources}
    )
    return {
        "schema_version": "2.0-incremental",
        "mode": mode,
        "provider": provider.describe(),
        "person_folder": inventory.person_folder,
        "incremental": scan.as_dict(),
        "same_date_tie_break": SAME_DATE_TIE_BREAK,
        "sources": sources_list,
        "source_lifecycle": [item.as_dict() for item in source_lifecycle],
        "documents": docs,
        "summary": {
            # Các count báo cáo lấy từ canonical effective state.  Không dùng
            # classification_kind thô trước resolve, và không dùng số artifact
            # review lịch sử làm REVIEW_PENDING hiện tại.
            **canonical_summary,
        },
        "qc": qc.as_dict(),
        "failed_this_run": _failed_report(scan, failed),
        "notes": notes,
    }


def _finish_dry_run(
    *, inventory, ws, catalog, provider, scan, qc, notes, failed, write_manifest, state_registry
) -> PipelineResult:
    manifest = _incremental_manifest(
        catalog, inventory, provider, scan, qc, mode=MODE_DRY_RUN, failed=failed, notes=notes,
        state_registry=state_registry,
    )
    manifest_path = None
    if write_manifest:
        manifest_path = _persist(manifest, ws, inventory.person_folder, MODE_DRY_RUN)

    if not qc.passed:
        status = "BLOCKED_QC"
    elif manifest["summary"]["review_pending"] > 0:
        status = "REVIEW_REQUIRED"
    else:
        status = "DRY_RUN_PASS"

    return PipelineResult(
        person_folder=inventory.person_folder, mode=MODE_DRY_RUN, inventory=inventory, documents=[],
        qc=qc, manifest=manifest, manifest_path=manifest_path,
        notes=notes or (["Không có nguồn mới cần xử lý."] if not scan.needs_agent_sources else []),
        incremental=scan, status_override=status,
    )


def _finish_apply(
    *, inventory, ws, output_dir, review_dir, catalog, provider, scan, apply_sources, notes, failed,
    qc, naming_policy, state_registry, write_manifest,
) -> PipelineResult:
    person = inventory.person_folder

    if not qc.passed:
        manifest = _incremental_manifest(
            catalog, inventory, provider, scan, qc, mode=MODE_APPLY, failed=failed,
            notes=notes + ["QC (phân tích) không đạt -> không apply."], state_registry=state_registry,
        )
        path = _persist(manifest, ws, person, MODE_APPLY) if write_manifest else None
        return PipelineResult(
            person_folder=person, mode=MODE_APPLY, inventory=inventory, documents=[], qc=qc,
            manifest=manifest, manifest_path=path, notes=notes, incremental=scan, status_override="BLOCKED_QC",
        )

    if not apply_sources:
        manifest = _incremental_manifest(
            catalog, inventory, provider, scan, qc, mode=MODE_APPLY, failed=failed, notes=notes,
            state_registry=state_registry,
        )
        path = _persist(manifest, ws, person, MODE_APPLY) if write_manifest else None
        return PipelineResult(
            person_folder=person, mode=MODE_APPLY, inventory=inventory, documents=[], qc=qc,
            manifest=manifest, manifest_path=path,
            notes=notes or ["Không có nguồn mới cần xử lý."], incremental=scan, status_override="APPLY_PASS",
        )

    # 1) Loại type_id "active" TAXONOMY + nhóm SUPPORTING_DOCUMENT "active"
    #    (xuất hiện trong logical document settled của các nguồn apply lượt
    #    này). DUPLICATE (Policy 3) không bao giờ vào naming pool nào - không
    #    tạo output riêng (`is_nameable`).
    active_types: set[str] = set()
    active_supporting_keys: set[str] = set()
    for s in apply_sources:
        for row in state_registry.logical_documents_for(s.sha256):
            if not row.is_settled or not row.is_nameable:
                continue
            kind = row.effective_classification_kind
            if kind == CLASSIFICATION_KIND_TAXONOMY:
                active_types.add(row.effective_type_id)
            elif kind == CLASSIFICATION_KIND_SUPPORTING:
                active_supporting_keys.add(supporting_group_key(row.title_short))

    # 2) Global assignment (Phase F/G) + rename plan (Phase H/I) cho mỗi type -
    #    nhìn TOÀN BỘ hồ sơ người này, không chỉ apply_sources lượt này (vd
    #    chèn tài liệu cũ hơn có thể phải đổi tên file của nguồn đã PROCESSED
    #    từ trước).
    all_ops = []
    assignment_by_type: dict[str, list] = {}
    unorderable_types: set[str] = set()
    for type_id in sorted(active_types):
        rows = [
            r for r in state_registry.logical_documents_for_person(
                person, type_id=type_id, include_retired=False
            )
            if r.is_settled and r.is_nameable and r.effective_classification_kind == CLASSIFICATION_KIND_TAXONOMY
        ]
        nameable = [NameableDoc.from_row(r) for r in rows]
        assignment, reasons = compute_global_assignment(catalog, type_id, nameable, naming_policy)
        assignment_by_type[type_id] = assignment
        if reasons:
            unorderable_types.add(type_id)
            notes.append(
                f"Loại {type_id}: chưa xếp được thứ tự toàn cục ({', '.join(reasons)}) "
                "-> giữ nguyên tên hiện có, chưa hoàn tất."
            )
            continue
        current = {
            r.logical_document_id: (r.current_target_filename, r.target_dir)
            for r in rows if r.current_target_filename
        }
        all_ops.extend(build_rename_plan(current, assignment))

    # 2b) SUPPORTING_DOCUMENT (Policy 2/5) - nhóm theo TIÊU ĐỀ chuẩn hoá, không
    #     theo type_id (không có type_id chính thức, không dùng STT 01-104
    #     giả). Luôn xếp được (không cần ngày) - chỉ tie-break xác định.
    if active_supporting_keys:
        all_person_rows = state_registry.logical_documents_for_person(
            person, include_retired=False
        )
        for key in sorted(active_supporting_keys):
            rows = [
                r for r in all_person_rows
                if r.is_settled and r.is_nameable
                and r.effective_classification_kind == CLASSIFICATION_KIND_SUPPORTING
                and supporting_group_key(r.title_short) == key
            ]
            nameable = [NameableDoc.from_row(r) for r in rows]
            assignment = compute_supporting_assignment(nameable)
            assignment_by_type[f"__supporting__:{key}"] = assignment
            current = {
                r.logical_document_id: (r.current_target_filename, r.target_dir)
                for r in rows if r.current_target_filename
            }
            all_ops.extend(build_rename_plan(current, assignment))

    collisions = has_collisions(all_ops)
    if collisions:
        for s in apply_sources:
            state_registry.mark_failed(
                s.sha256, error="Xung đột kế hoạch đặt tên toàn cục: " + "; ".join(collisions)
            )
        qc.add("global_naming_plan", False, "; ".join(collisions))
        manifest = _incremental_manifest(
            catalog, inventory, provider, scan, qc, mode=MODE_APPLY, failed={**failed, **{s.sha256: "collision" for s in apply_sources}},
            notes=notes, state_registry=state_registry,
        )
        path = _persist(manifest, ws, person, MODE_APPLY) if write_manifest else None
        return PipelineResult(
            person_folder=person, mode=MODE_APPLY, inventory=inventory, documents=[], qc=qc,
            manifest=manifest, manifest_path=path, notes=notes, incremental=scan, status_override="BLOCKED_QC",
        )

    # 3) PDF nguồn cho các CREATE op (tài liệu hoàn toàn mới, chưa có file).
    source_path_of: dict[str, Path] = {}
    pages_of: dict[str, list[int]] = {}
    for op in all_ops:
        if op.kind != "CREATE":
            continue
        row = state_registry.get_logical_document(op.logical_document_id)
        src_state = state_registry.get(row.source_hash)
        src_file = inventory.by_name(src_state.source_filename)
        source_path_of[op.logical_document_id] = src_file.path
        pages_of[op.logical_document_id] = row.source_pages

    # 4) Thực thi rename plan - fail-safe, không đổi gì cả nếu lỗi giữa chừng.
    try:
        if all_ops:
            execute_rename_plan(
                output_dir, review_dir, all_ops, source_path_of=source_path_of, pages_of=pages_of
            )
    except PipelineError as exc:
        for s in apply_sources:
            state_registry.mark_failed(s.sha256, error=f"Ghi/đổi tên file thất bại: {exc}")
        qc.add("rename_execution", False, str(exc))
        manifest = _incremental_manifest(
            catalog, inventory, provider, scan, qc, mode=MODE_APPLY,
            failed={**failed, **{s.sha256: str(exc) for s in apply_sources}}, notes=notes,
            state_registry=state_registry,
        )
        path = _persist(manifest, ws, person, MODE_APPLY) if write_manifest else None
        return PipelineResult(
            person_folder=person, mode=MODE_APPLY, inventory=inventory, documents=[], qc=qc,
            manifest=manifest, manifest_path=path, notes=notes, incremental=scan, status_override="BLOCKED_RUNTIME",
        )

    # 5) Ghi bản REVIEW_PENDING ra review/ (tên cách ly, không phải tên chính thức).
    _write_review_copies(state_registry, catalog, inventory, apply_sources, review_dir)

    # 6) Cập nhật DB target cho các logical document vừa xếp được thứ tự.
    for type_id, assignment in assignment_by_type.items():
        if type_id in unorderable_types:
            continue
        for a in assignment:
            state_registry.set_target(
                a.logical_document_id, target_filename=a.target_filename,
                target_dir="output", sequence_index=a.sequence_index,
            )

    # 7) QC cuối: nguồn không đổi + file đầu ra đọc được đúng số trang.
    mutations = verify_unchanged(inventory)
    qc.add("source_unchanged", not mutations, "; ".join(mutations) or "SHA-256 file nguồn giữ nguyên")
    output_problems = _verify_outputs(state_registry, apply_sources, output_dir, review_dir)
    qc.add(
        "outputs_readable", not output_problems,
        "; ".join(output_problems) or "mọi file đầu ra mở được và đúng số trang",
    )
    qc.add("global_naming_plan", True, f"{len(all_ops)} thao tác đặt tên/đổi tên đã thực thi thành công")

    # 8) Chuyển trạng thái từng nguồn: PROCESSED chỉ khi không còn REVIEW_PENDING
    #    VÀ mọi logical document đã settled đều có filename chính thức.
    if qc.passed:
        for s in apply_sources:
            rows = state_registry.logical_documents_for(s.sha256)
            pending = [r for r in rows if r.resolution_status == "REVIEW_PENDING"]
            # DUPLICATE (Policy 3) không bao giờ có filename riêng - không tính
            # vào điều kiện "đã có tên" (`is_nameable` = False cho DUPLICATE).
            all_settled_named = all(
                (not r.is_nameable) or r.current_target_filename for r in rows if r.is_settled
            )
            if not pending and all_settled_named:
                state_registry.commit_processed(
                    s.sha256, logical_document_count=len(rows), manifest_path=str(output_dir / "_manifest.json")
                )

    manifest = _incremental_manifest(
        catalog, inventory, provider, scan, qc, mode=MODE_APPLY, failed=failed, notes=notes,
        state_registry=state_registry,
    )
    manifest_path = None
    if write_manifest:
        manifest_path = _persist(manifest, ws, person, MODE_APPLY)
        save_manifest(manifest, output_dir / "_manifest.json")

    return PipelineResult(
        person_folder=person, mode=MODE_APPLY, inventory=inventory, documents=[], qc=qc,
        manifest=manifest, manifest_path=manifest_path, notes=notes, incremental=scan,
        status_override="APPLY_PASS" if qc.passed else "BLOCKED_QC",
    )


def _write_review_copies(
    state_registry: StateRegistry,
    catalog: Catalog,
    inventory: PersonInventory,
    apply_sources: list[SourceFile],
    review_dir: Path,
) -> None:
    for s in apply_sources:
        for row in state_registry.logical_documents_for(s.sha256):
            if row.resolution_status != "REVIEW_PENDING" or row.current_target_filename:
                continue
            name = review_filename(catalog, row.type_id, s.name, row.source_pages)
            target = review_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                split_pages(s.path, row.source_pages, target)
            state_registry.set_target(
                row.logical_document_id, target_filename=name, target_dir="review", sequence_index=None
            )


def _verify_outputs(
    state_registry: StateRegistry, apply_sources: list[SourceFile], output_dir: Path, review_dir: Path
) -> list[str]:
    problems: list[str] = []
    for s in apply_sources:
        for row in state_registry.logical_documents_for(s.sha256):
            if not row.current_target_filename:
                continue
            base = output_dir if row.target_dir == "output" else review_dir
            path = base / row.current_target_filename
            if not path.is_file():
                problems.append(f"Thiếu file đầu ra: {path}")
                continue
            try:
                n = len(PdfReader(str(path)).pages)
            except Exception as exc:
                problems.append(f"Không mở được file đầu ra '{path.name}': {exc}")
                continue
            if n != len(row.source_pages):
                problems.append(f"'{path.name}' có {n} trang, kỳ vọng {len(row.source_pages)}.")
    return problems


FLAG_AGENT_SEGMENTATION_MISMATCH = "AGENT_SEGMENTATION_MISMATCH"


def cross_check_segmentation(logical_docs: list, proposed: list[list[int]]) -> list:
    """Gắn cờ những logical document mà Agent gom khác segmenter local.

    Trả về danh sách các document bị gắn cờ (sửa tại chỗ `segmentation_flags`).
    """
    proposed_set = {tuple(g) for g in proposed}
    flagged = []
    for d in logical_docs:
        if tuple(d.source_pages) not in proposed_set:
            d.segmentation_flags = sorted(
                set(d.segmentation_flags) | {FLAG_AGENT_SEGMENTATION_MISMATCH}
            )
            flagged.append(d)
    return flagged


def _persist(manifest: dict, ws: Workspace, person: str, mode: str) -> Path:
    name = "manifest.dryrun.json" if mode == MODE_DRY_RUN else "manifest.apply.json"
    return save_manifest(manifest, ws.logs / person / name)
