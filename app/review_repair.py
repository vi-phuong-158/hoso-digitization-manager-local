"""Review & Repair subsystem.

Review is evidence-first: deterministic checks run locally, optional semantic
reviewers only add proposed findings, and canonical logical-document state is
changed only by an explicit human decision and a separate repair plan.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .catalog import Catalog
from .global_naming import (
    NameableDoc, build_rename_plan, compute_global_assignment,
    compute_supporting_assignment, execute_rename_plan, has_collisions,
)
from .models import PipelineError
from .pdf_inventory import sha256_file
from .policy import (
    CLASSIFICATION_KIND_DUPLICATE, CLASSIFICATION_KIND_SUPPORTING,
    CLASSIFICATION_KIND_TAXONOMY, supporting_group_key,
)
from .state import LogicalDocumentRow, StateRegistry, _now, logical_document_id


FINDING_TYPES = frozenset({
    "WRONG_CLASSIFICATION", "WRONG_DOCUMENT_BOUNDARY", "SHOULD_MERGE",
    "SHOULD_SPLIT", "MISSING_DOCUMENT", "EXTRA_DOCUMENT", "WRONG_DUPLICATE",
    "MISSED_DUPLICATE", "WRONG_FILENAME", "WRONG_PAGE_ORDER",
    "LOW_CONFIDENCE", "IMAGE_QUALITY_PROBLEM", "UNKNOWN",
})
DECISIONS = frozenset({"ACCEPT", "KEEP_EXISTING", "MANUAL_FIX"})
SEVERITIES = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
PENDING = "PENDING_REVIEW"


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _u(value: Optional[str], default: Any) -> Any:
    return json.loads(value) if value else default


@dataclass(frozen=True)
class ReviewFinding:
    finding_id: str
    session_id: str
    source_hash: Optional[str]
    page_start: Optional[int]
    page_end: Optional[int]
    finding_type: str
    severity: str
    existing_result: dict[str, Any]
    proposed_result: dict[str, Any]
    reason: str
    confidence: float
    status: str
    evidence: dict[str, Any] | list[Any] | None = None
    fingerprint: Optional[str] = None
    reviewer_version: Optional[str] = None
    decision: Optional[str] = None
    decision_payload: Optional[dict[str, Any]] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None


@dataclass(frozen=True)
class ReviewSession:
    session_id: str
    person_folder: str
    base_revision: int
    finding_count: int
    review_status: str
    repair_status: str
    result_revision: Optional[int]


@dataclass(frozen=True)
class RepairPlan:
    repair_plan_id: str
    session_id: str
    person_folder: str
    base_revision: int
    status: str
    changes: list[dict[str, Any]]


def _conn(registry: StateRegistry) -> sqlite3.Connection:
    # The registry owns the connection and schema.  This module is the sole
    # review/repair domain consumer of these tables; no second state DB exists.
    return registry._conn


def _row_finding(row: sqlite3.Row) -> ReviewFinding:
    return ReviewFinding(
        finding_id=row["finding_id"], session_id=row["session_id"], source_hash=row["source_hash"],
        page_start=row["page_start"], page_end=row["page_end"], finding_type=row["finding_type"],
        severity=row["severity"], existing_result=_u(row["existing_result_json"], {}),
        proposed_result=_u(row["proposed_result_json"], {}), reason=row["reason"],
        confidence=float(row["confidence"]), status=row["status"], decision=row["decision"],
        decision_payload=_u(row["decision_payload_json"], None), reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"], evidence=_u(row["evidence_json"], None),
        fingerprint=row["fingerprint"], reviewer_version=row["reviewer_version"],
    )


def _snapshot(registry: StateRegistry, person_folder: str) -> dict[str, Any]:
    return {
        "sources": [s.as_dict() for s in registry.all(person_folder)],
        "logical_documents": [d.as_dict() for d in registry.logical_documents_for_person(person_folder)],
    }


def _base_revision(registry: StateRegistry, person_folder: str) -> int:
    conn = _conn(registry)
    row = conn.execute("SELECT MAX(revision) AS n FROM case_revisions WHERE person_folder = ?", (person_folder,)).fetchone()
    if row and row["n"] is not None:
        return int(row["n"])
    with conn:
        conn.execute(
            "INSERT INTO case_revisions(person_folder, revision, parent_revision, kind, summary, snapshot_json, created_by, created_at) VALUES (?,1,NULL,'ORIGINAL','Canonical state before Review & Repair',?,'system',?)",
            (person_folder, _j(_snapshot(registry, person_folder)), _now()),
        )
    return 1


def _in_scope(row: LogicalDocumentRow, source_hash: Optional[str], pages: Optional[tuple[int, int]]) -> bool:
    if source_hash and row.source_hash != source_hash:
        return False
    if pages is None:
        return True
    return any(pages[0] <= page <= pages[1] for page in row.source_pages)


def _finding(
    *, session_id: str, source_hash: Optional[str], pages: Iterable[int], finding_type: str,
    severity: str, existing: dict[str, Any], proposed: Optional[dict[str, Any]],
    reason: str, confidence: float, evidence: dict[str, Any] | list[Any] | None = None,
    fingerprint: Optional[str] = None, reviewer_version: Optional[str] = None,
) -> ReviewFinding:
    if finding_type not in FINDING_TYPES:
        raise PipelineError(f"review finding_type không hợp lệ: {finding_type}")
    page_list = sorted(set(pages))
    return ReviewFinding(
        finding_id=uuid.uuid4().hex, session_id=session_id, source_hash=source_hash,
        page_start=page_list[0] if page_list else None, page_end=page_list[-1] if page_list else None,
        finding_type=finding_type, severity=severity, existing_result=existing,
        proposed_result=proposed or {}, reason=reason[:2000], confidence=max(0.0, min(1.0, confidence)),
        status=PENDING, evidence=evidence, fingerprint=fingerprint, reviewer_version=reviewer_version,
    )


def deterministic_audit(
    registry: StateRegistry, person_folder: str, *, session_id: str, catalog: Catalog,
    output_dir: Path, review_dir: Path, source_hash: Optional[str] = None,
    pages: Optional[tuple[int, int]] = None,
) -> list[ReviewFinding]:
    """Run local checks before any semantic/AI reviewer is considered."""
    rows = [r for r in registry.logical_documents_for_person(person_folder) if _in_scope(r, source_hash, pages)]
    findings: list[ReviewFinding] = []
    by_source: dict[str, list[LogicalDocumentRow]] = {}
    for row in rows:
        by_source.setdefault(row.source_hash, []).append(row)
        existing = row.as_dict()
        if row.confidence < 0.95:
            findings.append(_finding(session_id=session_id, source_hash=row.source_hash, pages=row.source_pages,
                finding_type="LOW_CONFIDENCE", severity="MEDIUM", existing=existing, proposed=None,
                reason="Kết quả hiện hữu dưới ngưỡng AUTO 0.95; cần đánh giá ngữ nghĩa.", confidence=1.0 - row.confidence))
        if row.current_target_filename:
            base = output_dir if row.target_dir == "output" else review_dir
            artifact = base / row.current_target_filename
            if not artifact.is_file():
                findings.append(_finding(session_id=session_id, source_hash=row.source_hash, pages=row.source_pages,
                    finding_type="MISSING_DOCUMENT", severity="HIGH", existing=existing, proposed=None,
                    reason="State/ledger có logical document nhưng artifact đầu ra không còn trên đĩa.", confidence=1.0))
            elif row.effective_classification_kind == CLASSIFICATION_KIND_TAXONOMY:
                base_name = catalog.filename_base(row.effective_type_id)
                if not re.fullmatch(re.escape(base_name) + r"(?:\.\d+)?\.pdf", row.current_target_filename):
                    findings.append(_finding(session_id=session_id, source_hash=row.source_hash, pages=row.source_pages,
                        finding_type="WRONG_FILENAME", severity="MEDIUM", existing=existing, proposed={},
                        reason="Tên artifact không khớp filename_base trong taxonomy hiện hành.", confidence=1.0))
        if row.effective_classification_kind == CLASSIFICATION_KIND_DUPLICATE:
            target = registry.get_logical_document(row.duplicate_of or "")
            invalid = target is None or target.source_hash not in {s.source_hash for s in registry.all(person_folder)}
            if target and target.effective_classification_kind == CLASSIFICATION_KIND_DUPLICATE:
                invalid = True
            if invalid:
                findings.append(_finding(session_id=session_id, source_hash=row.source_hash, pages=row.source_pages,
                    finding_type="WRONG_DUPLICATE", severity="HIGH", existing=existing, proposed={},
                    reason="Quan hệ duplicate không trỏ tới logical document gốc hợp lệ trong cùng hồ sơ.", confidence=1.0))
    for hash_, source_rows in by_source.items():
        source = registry.get(hash_)
        if source is None:
            continue
        owners: dict[int, list[str]] = {}
        for row in source_rows:
            for page in row.source_pages:
                owners.setdefault(page, []).append(row.logical_document_id)
        missing = [p for p in range(1, source.page_count + 1) if p not in owners]
        if missing:
            findings.append(_finding(session_id=session_id, source_hash=hash_, pages=missing,
                finding_type="MISSING_DOCUMENT", severity="HIGH", existing={"source_hash": hash_}, proposed={},
                reason="Có trang nguồn chưa thuộc logical document nào.", confidence=1.0))
        overlap = [p for p, ids in owners.items() if len(ids) > 1]
        if overlap:
            findings.append(_finding(session_id=session_id, source_hash=hash_, pages=overlap,
                finding_type="WRONG_DOCUMENT_BOUNDARY", severity="HIGH", existing={"owners": owners}, proposed={},
                reason="Một hay nhiều trang nguồn đang được dùng bởi nhiều logical document.", confidence=1.0))
    known = {((output_dir if row.target_dir == "output" else review_dir) / row.current_target_filename).resolve()
             for row in rows if row.current_target_filename}
    for base in (output_dir, review_dir):
        if base.is_dir():
            for path in base.glob("*.pdf"):
                if path.resolve() not in known:
                    findings.append(_finding(session_id=session_id, source_hash=None, pages=[],
                        finding_type="EXTRA_DOCUMENT", severity="MEDIUM", existing={"path": str(path)}, proposed={},
                        reason="Artifact PDF không có logical document tương ứng trong state.", confidence=1.0))
    return findings


def start_review(
    registry: StateRegistry, person_folder: str, *, catalog: Catalog, output_dir: Path, review_dir: Path,
    source_hash: Optional[str] = None, pages: Optional[tuple[int, int]] = None,
    review_method: str = "deterministic", model_metadata: Optional[dict[str, Any]] = None,
) -> tuple[ReviewSession, list[ReviewFinding]]:
    if pages and (pages[0] < 1 or pages[1] < pages[0]):
        raise PipelineError("Phạm vi trang review không hợp lệ.")
    base = _base_revision(registry, person_folder)
    session_id, now = uuid.uuid4().hex, _now()
    scope = {"source_hash": source_hash, "pages": list(pages) if pages else None}
    with _conn(registry):
        _conn(registry).execute(
            "INSERT INTO review_sessions(session_id,person_folder,scope_json,base_revision,review_method,model_metadata_json,finding_count,review_status,repair_status,created_at) VALUES (?,?,?,?,?,?,0,'OPEN','NOT_PLANNED',?)",
            (session_id, person_folder, _j(scope), base, review_method, _j(model_metadata or {}), now),
        )
    findings = deterministic_audit(registry, person_folder, session_id=session_id, catalog=catalog,
        output_dir=output_dir, review_dir=review_dir, source_hash=source_hash, pages=pages)
    _insert_findings(registry, findings)
    with _conn(registry):
        _conn(registry).execute("UPDATE review_sessions SET finding_count=? WHERE session_id=?", (len(findings), session_id))
    return ReviewSession(session_id, person_folder, base, len(findings), "OPEN", "NOT_PLANNED", None), findings


def _insert_findings(registry: StateRegistry, findings: Iterable[ReviewFinding]) -> None:
    conn = _conn(registry)
    with conn:
        for f in findings:
            conn.execute("""INSERT INTO review_findings(finding_id,session_id,source_hash,page_start,page_end,finding_type,severity,existing_result_json,proposed_result_json,reason,confidence,status,evidence_json,fingerprint,reviewer_version,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (f.finding_id, f.session_id, f.source_hash, f.page_start, f.page_end,
                f.finding_type, f.severity, _j(f.existing_result), _j(f.proposed_result), f.reason, f.confidence, f.status,
                _j(f.evidence) if f.evidence is not None else None, f.fingerprint, f.reviewer_version, _now()))


def _semantic_fingerprint(raw: dict[str, Any], *, base_revision: int, reviewer_version: str) -> str:
    canonical = {key: raw.get(key) for key in ("source_hash", "source_pages", "finding_type", "existing_result", "proposed_result", "evidence")}
    return hashlib.sha256(_j({"base_revision": base_revision, "reviewer_version": reviewer_version, "finding": canonical}).encode("utf-8")).hexdigest()


def _validate_semantic_finding(registry: StateRegistry, session: sqlite3.Row, catalog: Catalog, raw: dict[str, Any], reviewer_version: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PipelineError("Semantic reviewer trả finding không phải JSON object.")
    kind = raw.get("finding_type")
    if kind not in FINDING_TYPES:
        raise PipelineError(f"Semantic reviewer trả finding_type không hợp lệ: {kind!r}")
    if raw.get("severity", "MEDIUM") not in SEVERITIES:
        raise PipelineError("Semantic finding severity không hợp lệ.")
    source_hash, pages = raw.get("source_hash"), raw.get("source_pages")
    if not isinstance(source_hash, str) or registry.get(source_hash) is None:
        raise PipelineError("Semantic finding phải chỉ tới source_hash hiện hữu.")
    source = registry.get(source_hash)
    if not isinstance(pages, list) or not pages or any(not isinstance(p, int) for p in pages) or pages != sorted(set(pages)) or pages[0] < 1 or pages[-1] > source.page_count:
        raise PipelineError("Semantic finding có source_pages không hợp lệ.")
    scope = _u(session["scope_json"], {})
    if scope.get("source_hash") and scope["source_hash"] != source_hash:
        raise PipelineError("Semantic finding nằm ngoài source scope được chọn.")
    if scope.get("pages") and (pages[0] < scope["pages"][0] or pages[-1] > scope["pages"][1]):
        raise PipelineError("Semantic finding nằm ngoài page scope được chọn.")
    existing, proposed, evidence = raw.get("existing_result"), raw.get("proposed_result"), raw.get("evidence")
    if not isinstance(existing, dict) or not isinstance(proposed, dict) or not isinstance(evidence, (dict, list)):
        raise PipelineError("Semantic finding thiếu existing_result, proposed_result hoặc evidence có cấu trúc.")
    ids = existing.get("logical_document_id")
    requested_ids = ([ids] if isinstance(ids, str) else existing.get("document_ids", []))
    if requested_ids and (not isinstance(requested_ids, list) or any(registry.get_logical_document(x) is None for x in requested_ids)):
        raise PipelineError("Semantic finding chứa logical_document_id không tồn tại.")
    if any(registry.get_logical_document(x).source_hash != source_hash for x in requested_ids):
        raise PipelineError("Semantic finding không được tham chiếu logical document thuộc source khác.")
    proposed_documents = proposed.get("documents", [])
    if not isinstance(proposed_documents, list):
        raise PipelineError("Semantic proposed.documents phải là mảng.")
    for payload in [proposed, *proposed_documents]:
        if not isinstance(payload, dict):
            raise PipelineError("Semantic proposed documents phải là objects.")
        type_id = payload.get("type_id")
        if type_id is not None and not catalog.is_valid_classification(str(type_id)):
            raise PipelineError("Semantic finding đề xuất type_id ngoài taxonomy.")
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise PipelineError("Semantic finding confidence không hợp lệ.") from exc
    if not 0 <= confidence <= 1 or not isinstance(raw.get("reason"), str) or not raw["reason"].strip() or len(raw["reason"]) > 2000:
        raise PipelineError("Semantic finding reason/confidence không hợp lệ.")
    if kind in {"SHOULD_MERGE", "WRONG_DOCUMENT_BOUNDARY"} and (len(proposed.get("document_ids", existing.get("document_ids", []))) < 2 or not proposed.get("source_pages")):
        raise PipelineError("Semantic merge/boundary proposal cần document_ids và source_pages.")
    if kind == "SHOULD_SPLIT" and (not proposed_documents or any(not item.get("source_pages") for item in proposed_documents)):
        raise PipelineError("Semantic split proposal cần documents/source_pages.")
    if kind == "MISSING_DOCUMENT" and (not proposed.get("source_pages") or not proposed.get("type_id")):
        raise PipelineError("Semantic missing-document proposal cần source_pages/type_id.")
    if kind == "EXTRA_DOCUMENT" and not requested_ids:
        raise PipelineError("Semantic extra-document proposal cần logical_document_id.")
    return {**raw, "confidence": confidence, "reviewer_version": reviewer_version,
            "fingerprint": _semantic_fingerprint(raw, base_revision=int(session["base_revision"]), reviewer_version=reviewer_version)}


def record_semantic_findings(registry: StateRegistry, session_id: str, findings: Iterable[dict[str, Any]], *, catalog: Catalog | None = None, reviewer_version: str = "manual-proposal.v1") -> list[ReviewFinding]:
    """Persist reviewer proposals only. This function has no repair capability."""
    session = _conn(registry).execute("SELECT * FROM review_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not session:
        raise PipelineError("review session không tồn tại.")
    catalog = catalog or __import__("app.catalog", fromlist=["load_catalog"]).load_catalog()
    out: list[ReviewFinding] = []
    signatures: dict[tuple[str, str], str] = {}
    for raw in findings:
        raw = _validate_semantic_finding(registry, session, catalog, raw, reviewer_version)
        logical_ids = raw["existing_result"].get("document_ids") or [raw["existing_result"].get("logical_document_id")]
        for logical_id in filter(None, logical_ids):
            signature = (raw["finding_type"], logical_id)
            proposal = _j(raw["proposed_result"])
            if signature in signatures and signatures[signature] != proposal:
                raise PipelineError("Semantic reviewer trả các proposal xung đột cho cùng finding scope.")
            signatures[signature] = proposal
        suppressed = _conn(registry).execute("SELECT 1 FROM review_findings WHERE fingerprint=? AND reviewer_version=? AND decision='KEEP_EXISTING' LIMIT 1", (raw["fingerprint"], reviewer_version)).fetchone()
        if suppressed:
            continue
        kind = raw["finding_type"]
        out.append(_finding(session_id=session_id, source_hash=raw["source_hash"], pages=raw["source_pages"],
            finding_type=kind, severity=raw.get("severity", "MEDIUM"), existing=raw.get("existing_result") or {},
            proposed=raw.get("proposed_result") or {}, reason=str(raw.get("reason") or "Không có lý do."),
            confidence=raw["confidence"], evidence=raw["evidence"], fingerprint=raw["fingerprint"], reviewer_version=reviewer_version))
    _insert_findings(registry, out)
    with _conn(registry):
        _conn(registry).execute("UPDATE review_sessions SET finding_count=finding_count+? WHERE session_id=?", (len(out), session_id))
    return out


def run_semantic_review(registry: StateRegistry, person_folder: str, *, catalog: Catalog, folder: Path, output_dir: Path, review_dir: Path, reviewer: Any, renderer: Any, source_hash: Optional[str] = None, pages: Optional[tuple[int, int]] = None) -> tuple[ReviewSession, list[ReviewFinding]]:
    """Run an explicit, scoped model review. No model output can mutate canonical state."""
    session, _ = start_review(registry, person_folder, catalog=catalog, output_dir=output_dir, review_dir=review_dir,
        source_hash=source_hash, pages=pages, review_method="semantic", model_metadata={"reviewer_version": reviewer.reviewer_version, "prompt_version": "review-existing-result.v1"})
    sources = [s for s in registry.all(person_folder) if not source_hash or s.source_hash == source_hash]
    persisted: list[ReviewFinding] = []
    for source in sources:
        selected = list(range((pages or (1, source.page_count))[0], (pages or (1, source.page_count))[1] + 1))
        relevant_docs = [d.as_dict() for d in registry.logical_documents_for(source.source_hash) if _in_scope(d, source.source_hash, pages)]
        candidate_types = sorted({d["resolved_type_id"] or d["type_id"] for d in relevant_docs if (d["resolved_type_id"] or d["type_id"]) in catalog})
        request = __import__("app.semantic_reviewer", fromlist=["SemanticReviewRequest"]).SemanticReviewRequest(
            source_hash=source.source_hash, source_filename=source.source_filename,
            rendered_pages=renderer.render(folder / source.source_filename, selected), documents=relevant_docs,
            taxonomy=catalog.describe(candidate_types), reviewer_version=reviewer.reviewer_version,
        )
        persisted.extend(record_semantic_findings(registry, session.session_id, reviewer.review(request), catalog=catalog, reviewer_version=reviewer.reviewer_version))
    return session, persisted


def list_findings(registry: StateRegistry, session_id: str) -> list[ReviewFinding]:
    rows = _conn(registry).execute("SELECT * FROM review_findings WHERE session_id=? ORDER BY created_at,finding_id", (session_id,)).fetchall()
    return [_row_finding(row) for row in rows]


def sessions_for_person(registry: StateRegistry, person_folder: str) -> list[ReviewSession]:
    rows = _conn(registry).execute(
        "SELECT * FROM review_sessions WHERE person_folder=? ORDER BY created_at DESC", (person_folder,)
    ).fetchall()
    return [ReviewSession(row["session_id"], row["person_folder"], int(row["base_revision"]),
        int(row["finding_count"]), row["review_status"], row["repair_status"], row["result_revision"]) for row in rows]


def decide_finding(registry: StateRegistry, finding_id: str, *, decision: str, reviewer: str, manual_fix: Optional[dict[str, Any]] = None) -> ReviewFinding:
    if decision not in DECISIONS:
        raise PipelineError("Quyết định chỉ có thể là ACCEPT, KEEP_EXISTING hoặc MANUAL_FIX.")
    if not reviewer or len(reviewer) > 200:
        raise PipelineError("reviewer không hợp lệ.")
    if decision == "MANUAL_FIX" and not manual_fix:
        raise PipelineError("MANUAL_FIX cần payload sửa thủ công có cấu trúc.")
    row = _conn(registry).execute("SELECT * FROM review_findings WHERE finding_id=?", (finding_id,)).fetchone()
    if not row:
        raise PipelineError("finding không tồn tại.")
    if row["decision"]:
        raise PipelineError("finding đã có quyết định; không được ghi đè lịch sử.")
    with _conn(registry):
        _conn(registry).execute("UPDATE review_findings SET status='DECIDED',decision=?,decision_payload_json=?,reviewed_by=?,reviewed_at=? WHERE finding_id=?",
            (decision, _j(manual_fix or {}), reviewer, _now(), finding_id))
    return _row_finding(_conn(registry).execute("SELECT * FROM review_findings WHERE finding_id=?", (finding_id,)).fetchone())


def create_repair_plan(registry: StateRegistry, session_id: str) -> RepairPlan:
    session = _conn(registry).execute("SELECT * FROM review_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not session:
        raise PipelineError("review session không tồn tại.")
    existing = _conn(registry).execute("SELECT * FROM repair_plans WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
    if existing:
        return _plan_row(existing)
    changes = []
    for finding in list_findings(registry, session_id):
        if finding.decision not in {"ACCEPT", "MANUAL_FIX"}:
            continue
        proposed = dict(finding.proposed_result)
        if finding.decision == "MANUAL_FIX":
            proposed.update(finding.decision_payload or {})
        changes.append({"finding_id": finding.finding_id, "finding_type": finding.finding_type,
                        "source_hash": finding.source_hash, "pages": [finding.page_start, finding.page_end],
                        "existing": finding.existing_result, "proposed": proposed,
                        "decision": finding.decision, "reviewer": finding.reviewed_by})
    plan_id = uuid.uuid4().hex
    plan = {"affected_pages": sorted({(c["source_hash"], p) for c in changes for p in range((c["pages"][0] or 0), (c["pages"][1] or -1) + 1)}),
            "changes": changes, "dry_run": True}
    with _conn(registry):
        _conn(registry).execute("INSERT INTO repair_plans(repair_plan_id,session_id,person_folder,base_revision,plan_json,status,created_at) VALUES (?,?,?,?,?,'READY',?)",
            (plan_id, session_id, session["person_folder"], session["base_revision"], _j(plan), _now()))
        _conn(registry).execute("UPDATE review_sessions SET repair_status='PLANNED' WHERE session_id=?", (session_id,))
    return RepairPlan(plan_id, session_id, session["person_folder"], int(session["base_revision"]), "READY", changes)


def _plan_row(row: sqlite3.Row) -> RepairPlan:
    return RepairPlan(row["repair_plan_id"], row["session_id"], row["person_folder"], int(row["base_revision"]), row["status"], _u(row["plan_json"], {}).get("changes", []))


def get_repair_plan(registry: StateRegistry, plan_id: str) -> RepairPlan:
    row = _conn(registry).execute("SELECT * FROM repair_plans WHERE repair_plan_id=?", (plan_id,)).fetchone()
    if not row:
        raise PipelineError("repair plan không tồn tại.")
    return _plan_row(row)


def _replacement(old: LogicalDocumentRow, proposed: dict[str, Any], *, pages: Optional[list[int]] = None) -> dict[str, Any]:
    d = old.as_dict()
    d.update({k: v for k, v in proposed.items() if k in {
        "type_id", "document_date", "title_short", "classification_kind", "duplicate_of", "subtype", "date_precision"}})
    page_identity_changed = pages is not None and list(pages) != list(old.source_pages)
    if pages is not None:
        d["source_pages"] = pages
        d["logical_document_id"] = logical_document_id(old.source_hash, pages)
    if d.get("classification_kind") == CLASSIFICATION_KIND_DUPLICATE:
        d["resolved_classification_kind"] = CLASSIFICATION_KIND_DUPLICATE
        d["resolved_type_id"] = None
        d["current_target_filename"] = None
        d["target_dir"] = None
        d["sequence_index"] = None
    else:
        d["classification_kind"] = d.get("classification_kind") or CLASSIFICATION_KIND_TAXONOMY
        d["resolved_classification_kind"] = d["classification_kind"]
        d["resolved_type_id"] = d.get("type_id")
    # Preserve known date semantics when a repair only changes classification.
    d["resolved_document_date"] = d.get("document_date")
    d["resolved_date_precision"] = d.get("date_precision") or ("DAY" if d.get("document_date") else "UNKNOWN")
    d["classification_status"] = "AUTO"; d["resolution_status"] = "REVIEW_RESOLVED"
    d["classification_reasons"] = sorted(set(d.get("classification_reasons") or []) | {"REPAIR_APPROVED"})
    d["resolved_by"] = "review-repair"; d["resolved_at"] = _now(); d["updated_at"] = _now()
    # A reclassification keeps its stable identity and lets the existing
    # two-phase rename plan move the artifact. A changed page identity must
    # create a new artifact and has no inherited filename.
    if page_identity_changed:
        d["current_target_filename"] = None
        d["target_dir"] = None
        d["sequence_index"] = None
    return d


def _desired_documents(registry: StateRegistry, plan: RepairPlan, catalog: Catalog) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    all_rows = registry.logical_documents_for_person(plan.person_folder)
    by_id = {row.logical_document_id: row for row in all_rows}
    # ``as_dict`` is presentation-oriented and substitutes UNKNOWN for a
    # missing resolved precision.  Keep the nullable DB value here so a
    # targeted repair cannot accidentally override a reliable source date.
    desired: dict[str, list[dict[str, Any]]] = {}
    for row in all_rows:
        item = row.as_dict()
        item["resolved_date_precision"] = row.resolved_date_precision
        desired.setdefault(row.source_hash, []).append(item)
    touched: set[str] = set()
    for change in plan.changes:
        ex, prop = change["existing"], change["proposed"]
        lid = prop.get("logical_document_id") or ex.get("logical_document_id")
        kind = change["finding_type"]
        if kind in {"WRONG_CLASSIFICATION", "WRONG_FILENAME", "LOW_CONFIDENCE", "WRONG_DUPLICATE", "MISSED_DUPLICATE"}:
            if lid not in by_id:
                raise PipelineError(f"Repair finding {change['finding_id']} không xác định logical_document_id hiện hữu.")
            old = by_id[lid]; touched.add(old.source_hash)
            desired[old.source_hash] = [_replacement(old, prop) if d["logical_document_id"] == lid else d for d in desired[old.source_hash]]
        elif kind == "WRONG_PAGE_ORDER":
            if lid not in by_id:
                raise PipelineError("Page-order repair cần logical_document_id hiện hữu.")
            old = by_id[lid]; corrected_pages = list(prop.get("source_pages") or [])
            if sorted(corrected_pages) != sorted(old.source_pages):
                raise PipelineError("Page-order repair chỉ được chuẩn hóa thứ tự trang cùng source; không được đổi nội dung document.")
            touched.add(old.source_hash)
            desired[old.source_hash] = [_replacement(old, prop, pages=corrected_pages) if d["logical_document_id"] == lid else d for d in desired[old.source_hash]]
        elif kind in {"SHOULD_MERGE", "WRONG_DOCUMENT_BOUNDARY"}:
            members = prop.get("document_ids") or ex.get("document_ids") or ([lid] if lid else [])
            if not members or any(mid not in by_id for mid in members):
                raise PipelineError("Merge/boundary repair cần document_ids hợp lệ.")
            olds = [by_id[mid] for mid in members]
            if len({row.source_hash for row in olds}) != 1:
                raise PipelineError("Không được merge tài liệu thuộc các PDF nguồn khác nhau.")
            source = olds[0].source_hash; touched.add(source)
            new_pages = prop.get("source_pages") or sorted(p for row in olds for p in row.source_pages)
            replacement = _replacement(olds[0], prop, pages=list(new_pages))
            desired[source] = [d for d in desired[source] if d["logical_document_id"] not in set(members)] + [replacement]
        elif kind == "SHOULD_SPLIT":
            if lid not in by_id or not isinstance(prop.get("documents"), list) or not prop["documents"]:
                raise PipelineError("Split repair cần logical_document_id và proposed.documents.")
            old = by_id[lid]; touched.add(old.source_hash)
            pieces = [_replacement(old, piece, pages=list(piece.get("source_pages") or [])) for piece in prop["documents"]]
            if any(not piece["source_pages"] for piece in pieces):
                raise PipelineError("Mỗi phần split phải có source_pages.")
            desired[old.source_hash] = [d for d in desired[old.source_hash] if d["logical_document_id"] != lid] + pieces
        elif kind == "MISSING_DOCUMENT":
            source_hash = prop.get("source_hash") or change.get("source_hash")
            source = registry.get(source_hash or "")
            new_pages = list(prop.get("source_pages") or [])
            if source is None or not new_pages or not catalog.is_valid_classification(prop.get("type_id")):
                raise PipelineError("Add missing document cần source_hash, source_pages và type_id taxonomy hợp lệ.")
            # Use a source-local document as a metadata template; this preserves
            # all state invariants while only adding the uncovered pages.
            template = next((d for d in desired.get(source.source_hash, []) if d["source_hash"] == source.source_hash), None)
            if template is None:
                raise PipelineError("Không thể thêm missing document khi source chưa có metadata canonical.")
            new_doc = dict(template)
            new_doc.update({"source_pages": new_pages, "logical_document_id": logical_document_id(source.source_hash, new_pages),
                            "type_id": prop["type_id"], "resolved_type_id": prop["type_id"],
                            "title_short": prop.get("title_short"), "document_date": prop.get("document_date"),
                            "resolved_document_date": prop.get("document_date"), "current_target_filename": None,
                            "target_dir": None, "sequence_index": None, "classification_kind": CLASSIFICATION_KIND_TAXONOMY,
                            "resolved_classification_kind": CLASSIFICATION_KIND_TAXONOMY, "duplicate_of": None})
            desired.setdefault(source.source_hash, []).append(new_doc); touched.add(source.source_hash)
        elif kind == "EXTRA_DOCUMENT":
            if lid not in by_id:
                raise PipelineError("Remove extra document cần logical_document_id hiện hữu.")
            old = by_id[lid]; touched.add(old.source_hash)
            desired[old.source_hash] = [d for d in desired[old.source_hash] if d["logical_document_id"] != lid]
        else:
            raise PipelineError(f"Repair loại {kind} cần payload thủ công cụ thể; hệ thống từ chối suy đoán.")
    for source_hash, docs in desired.items():
        if source_hash not in touched:
            continue
        source = registry.get(source_hash)
        seen: set[int] = set()
        for d in docs:
            pgs = d["source_pages"]
            if pgs != sorted(set(pgs)) or any(p < 1 or p > source.page_count for p in pgs) or seen.intersection(pgs):
                raise PipelineError("Repair tạo boundary không hợp lệ hoặc overlap; canonical state không bị đổi.")
            seen.update(pgs)
            if d.get("classification_kind", CLASSIFICATION_KIND_TAXONOMY) == CLASSIFICATION_KIND_TAXONOMY and not catalog.is_valid_classification(d["type_id"]):
                raise PipelineError("Repair type_id không thuộc taxonomy chính thức.")
        if seen != set(range(1, source.page_count + 1)):
            raise PipelineError("Repair không cover 100% trang nguồn; dùng MANUAL_FIX đầy đủ hoặc giữ nguyên.")
    desired_ids = {d["logical_document_id"] for docs in desired.values() for d in docs}
    for docs in desired.values():
        for d in docs:
            if d.get("resolved_classification_kind") == CLASSIFICATION_KIND_DUPLICATE:
                target = d.get("duplicate_of")
                if not target or target == d["logical_document_id"] or target not in desired_ids:
                    raise PipelineError("Repair duplicate tạo self/dangling duplicate relation.")
                target_doc = next(x for group in desired.values() for x in group if x["logical_document_id"] == target)
                if target_doc.get("resolved_classification_kind") == CLASSIFICATION_KIND_DUPLICATE:
                    raise PipelineError("Repair duplicate không được tạo duplicate chain/cycle.")
    return desired, touched


def _insert_document(conn: sqlite3.Connection, d: dict[str, Any]) -> None:
    keys = ("logical_document_id", "source_hash", "source_pages", "type_id", "confidence", "document_date", "date_confidence", "title_short", "segmentation_flags", "classification_status", "classification_reasons", "resolution_status", "resolved_type_id", "resolved_document_date", "resolved_by", "resolved_at", "current_target_filename", "target_dir", "sequence_index", "created_at", "updated_at", "classification_kind", "subtype", "date_precision", "duplicate_of", "resolved_classification_kind", "resolved_subtype", "resolved_date_precision")
    values = []
    for key in keys:
        value = d.get(key)
        values.append(_j(value) if key in {"source_pages", "segmentation_flags", "classification_reasons"} else value)
    conn.execute(f"INSERT INTO logical_documents({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", values)


def _assign_all(registry: StateRegistry, person: str, catalog: Catalog, output_dir: Path, review_dir: Path, folder: Path) -> None:
    rows = [r for r in registry.logical_documents_for_person(person) if r.is_settled and r.is_nameable]
    assignments = []
    for type_id in sorted({r.effective_type_id for r in rows if r.effective_classification_kind == CLASSIFICATION_KIND_TAXONOMY}):
        group = [r for r in rows if r.effective_classification_kind == CLASSIFICATION_KIND_TAXONOMY and r.effective_type_id == type_id]
        assigned, reasons = compute_global_assignment(catalog, type_id, [NameableDoc.from_row(r) for r in group])
        if reasons:
            raise PipelineError(f"Repair cần đặt tên nhưng thứ tự loại {type_id} còn mơ hồ: {', '.join(reasons)}")
        assignments.extend(assigned)
    for key in sorted({supporting_group_key(r.title_short) for r in rows if r.effective_classification_kind == CLASSIFICATION_KIND_SUPPORTING}):
        group = [r for r in rows if r.effective_classification_kind == CLASSIFICATION_KIND_SUPPORTING and supporting_group_key(r.title_short) == key]
        assignments.extend(compute_supporting_assignment([NameableDoc.from_row(r) for r in group]))
    current = {r.logical_document_id: (r.current_target_filename, r.target_dir) for r in rows if r.current_target_filename}
    ops = build_rename_plan(current, assignments)
    if has_collisions(ops):
        raise PipelineError("Repair naming plan bị collision.")
    sources = {s.source_hash: folder / s.source_filename for s in registry.all(person)}
    execute_rename_plan(output_dir, review_dir, ops,
        source_path_of={op.logical_document_id: sources[registry.get_logical_document(op.logical_document_id).source_hash] for op in ops if op.kind == "CREATE"},
        pages_of={op.logical_document_id: registry.get_logical_document(op.logical_document_id).source_pages for op in ops if op.kind == "CREATE"})
    # Do not write unchanged rows.  Global naming must inspect the whole case,
    # but a targeted repair must not churn unrelated state/revision snapshots.
    by_id = {row.logical_document_id: row for row in registry.logical_documents_for_person(person)}
    for assignment in assignments:
        row = by_id[assignment.logical_document_id]
        if (row.current_target_filename, row.target_dir, row.sequence_index) == (assignment.target_filename, "output", assignment.sequence_index):
            continue
        _conn(registry).execute("UPDATE logical_documents SET current_target_filename=?,target_dir='output',sequence_index=?,updated_at=? WHERE logical_document_id=?", (assignment.target_filename, assignment.sequence_index, _now(), assignment.logical_document_id))


def _archive_retired_artifacts(
    old_rows: Iterable[LogicalDocumentRow], desired: dict[str, list[dict[str, Any]]],
    output_dir: Path, review_dir: Path, revision: int,
) -> list[tuple[Path, Path]]:
    """Retire superseded split/merge/duplicate artifacts without deleting them.

    The archive is nested so normal reconcile's top-level PDF inventory cannot
    mistake revision history for a live output.  Caller restores every move if
    the following rename/DB transaction fails.
    """
    wanted = {d["logical_document_id"]: d for docs in desired.values() for d in docs}
    moved: list[tuple[Path, Path]] = []
    try:
        for row in old_rows:
            replacement = wanted.get(row.logical_document_id)
            if replacement is not None and replacement.get("current_target_filename"):
                continue
            if not row.current_target_filename:
                continue
            base = output_dir if row.target_dir == "output" else review_dir
            source = base / row.current_target_filename
            if not source.is_file():
                continue
            destination = output_dir / "_revisions" / f"rev-{revision}" / f"{row.logical_document_id}.{row.current_target_filename}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append((source, destination))
    except Exception as exc:
        for source, destination in reversed(moved):
            try:
                os.replace(destination, source)
            except OSError:
                pass
        raise PipelineError(f"Không thể stage artifact retirement: {exc}") from exc
    return moved


def apply_repair(registry: StateRegistry, plan_id: str, *, catalog: Catalog, folder: Path, output_dir: Path, review_dir: Path, dry_run: bool = True) -> dict[str, Any]:
    plan = get_repair_plan(registry, plan_id)
    if plan.status == "APPLIED":
        return {"status": "ALREADY_APPLIED", "result_revision": _conn(registry).execute("SELECT result_revision FROM repair_plans WHERE repair_plan_id=?", (plan_id,)).fetchone()[0]}
    if plan.status != "READY":
        raise PipelineError(f"Repair plan ở trạng thái {plan.status}; cần recovery thủ công, không apply lại mù quáng.")
    current = _base_revision(registry, plan.person_folder)
    if current != plan.base_revision:
        raise PipelineError("STALE_REVIEW_BASE: canonical revision đã thay đổi kể từ review; cần review lại.")
    desired, touched = _desired_documents(registry, plan, catalog)
    result = {"status": "DRY_RUN" if dry_run else "READY", "repair_plan_id": plan_id,
              "affected_sources": sorted(touched), "affected_logical_documents": sum(len(desired[h]) for h in touched),
              "expected_revision": current + 1, "changes": plan.changes}
    if dry_run:
        return result
    before_hashes = {path: sha256_file(path) for path in Path(folder).glob("*.pdf")}
    conn = _conn(registry)
    with conn:
        conn.execute("UPDATE repair_plans SET status='APPLYING' WHERE repair_plan_id=?", (plan_id,))
    conn.execute("BEGIN")
    old_manifest = output_dir / "_manifest.json"
    old_manifest_bytes = old_manifest.read_bytes() if old_manifest.is_file() else None
    old_rows = [row for source_hash in touched for row in registry.logical_documents_for(source_hash)]
    archived: list[tuple[Path, Path]] = []
    try:
        for source_hash in touched:
            conn.execute("DELETE FROM logical_documents WHERE source_hash=?", (source_hash,))
            for document in desired[source_hash]:
                _insert_document(conn, document)
            conn.execute("UPDATE sources SET logical_document_count=?, updated_at=? WHERE source_hash=?", (len(desired[source_hash]), _now(), source_hash))
        archived = _archive_retired_artifacts(old_rows, desired, output_dir, review_dir, current + 1)
        # Global naming may include a minimal dependency closure when a target
        # filename is already held by another document.  This moves files
        # safely as one two-phase permutation; it never reprocesses source
        # pages outside the approved repair scope.
        _assign_all(registry, plan.person_folder, catalog, output_dir, review_dir, Path(folder))
        after = _snapshot(registry, plan.person_folder)
        revision = current + 1
        conn.execute("INSERT INTO case_revisions(person_folder,revision,parent_revision,kind,summary,snapshot_json,created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (plan.person_folder, revision, current, "REPAIR", f"Approved targeted repair {plan_id}", _j(after), "review-repair", _now()))
        for change in plan.changes:
            conn.execute("INSERT INTO correction_ledger(correction_id,repair_plan_id,finding_id,person_folder,source_hash,source_pages_json,finding_type,old_result_json,new_result_json,decision,reviewer,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, plan_id, change["finding_id"], plan.person_folder, change["source_hash"], _j(change["pages"]), change["finding_type"], _j(change["existing"]), _j(change["proposed"]), change["decision"], change["reviewer"] or "operator", _now()))
        manifest = {"schema_version": "2.0-incremental", "person_folder": plan.person_folder, "revision": revision,
                    "documents": after["logical_documents"], "sources": after["sources"], "repair_plan_id": plan_id}
        output_dir.mkdir(parents=True, exist_ok=True)
        tmp = output_dir / f".manifest.repair.{plan_id}.tmp"
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, old_manifest)
        conn.execute("UPDATE repair_plans SET status='APPLIED',applied_at=?,result_revision=? WHERE repair_plan_id=?", (_now(), revision, plan_id))
        conn.execute("UPDATE review_sessions SET review_status='COMPLETED',repair_status='APPLIED',result_revision=?,completed_at=? WHERE session_id=?", (revision, _now(), plan.session_id))
        conn.commit()
    except Exception:
        conn.rollback()
        for source, destination in reversed(archived):
            try:
                os.replace(destination, source)
            except OSError:
                pass
        if old_manifest_bytes is None:
            old_manifest.unlink(missing_ok=True)
        else:
            old_manifest.write_bytes(old_manifest_bytes)
        with conn:
            conn.execute("UPDATE repair_plans SET status='FAILED' WHERE repair_plan_id=?", (plan_id,))
        raise
    after_hashes = {path: sha256_file(path) for path in Path(folder).glob("*.pdf")}
    if before_hashes != after_hashes:
        raise PipelineError("SOURCE_MUTATION_DETECTED: repair đã dừng vì SHA-256 nguồn thay đổi.")
    result.update({"status": "APPLIED", "result_revision": current + 1})
    return result


def review_history(registry: StateRegistry, person_folder: str) -> list[dict[str, Any]]:
    rows = _conn(registry).execute("SELECT revision,parent_revision,kind,summary,created_by,created_at FROM case_revisions WHERE person_folder=? ORDER BY revision", (person_folder,)).fetchall()
    return [dict(row) for row in rows]


def revision_diff(registry: StateRegistry, person_folder: str, from_revision: int, to_revision: int) -> dict[str, Any]:
    def docs(revision: int) -> dict[str, Any]:
        row = _conn(registry).execute("SELECT snapshot_json FROM case_revisions WHERE person_folder=? AND revision=?", (person_folder, revision)).fetchone()
        if not row:
            raise PipelineError("revision không tồn tại.")
        return {d["logical_document_id"]: d for d in _u(row["snapshot_json"], {}).get("logical_documents", [])}
    a, b = docs(from_revision), docs(to_revision)
    return {"from_revision": from_revision, "to_revision": to_revision,
            "added": [b[k] for k in sorted(b.keys() - a.keys())], "removed": [a[k] for k in sorted(a.keys() - b.keys())],
            "changed": [{"before": a[k], "after": b[k]} for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]}


def correction_records(registry: StateRegistry, person_folder: str) -> list[dict[str, Any]]:
    rows = _conn(registry).execute(
        "SELECT * FROM correction_ledger WHERE person_folder=? ORDER BY created_at,correction_id", (person_folder,)
    ).fetchall()
    return [{**dict(row), "source_pages": _u(row["source_pages_json"], []),
             "old_result": _u(row["old_result_json"], {}), "new_result": _u(row["new_result_json"], {})}
            for row in rows]


def export_corrections_jsonl(registry: StateRegistry, person_folder: str, target: Path) -> Path:
    """Export durable corrections for local benchmarking/audit, never PDF data."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in correction_records(registry, person_folder)]
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return target


def benchmark_fixture(registry: StateRegistry, finding_id: str) -> dict[str, Any]:
    """Create an anonymized regression-fixture descriptor for one confirmed error."""
    row = _conn(registry).execute("SELECT * FROM review_findings WHERE finding_id=?", (finding_id,)).fetchone()
    if not row or row["decision"] not in {"ACCEPT", "MANUAL_FIX"}:
        raise PipelineError("Chỉ finding đã ACCEPT hoặc MANUAL_FIX mới có thể thành benchmark fixture.")
    finding = _row_finding(row)
    allowed_fixture_keys = {
        "type_id", "classification_kind", "classification_status",
        "resolution_status", "resolved_classification_kind",
    }
    old = {key: value for key, value in finding.existing_result.items() if key in allowed_fixture_keys}
    new = {key: value for key, value in finding.proposed_result.items() if key in allowed_fixture_keys}
    if finding.finding_type == "WRONG_FILENAME":
        # The canonical expected result is deliberately abstract: the naming
        # engine derives the concrete filename from the private catalog/state.
        new = {"naming": "catalog_deterministic"}
    return {"schema": "review-benchmark.v1", "fixture_id": f"review-{finding.finding_id}",
            "source": "anonymized", "pages": [finding.page_start, finding.page_end],
            "finding_type": finding.finding_type, "old_result": old, "expected_result": new,
            "decision": finding.decision}
