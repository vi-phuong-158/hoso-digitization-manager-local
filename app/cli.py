"""CLI vận hành.

    python -m app.cli process "input/<TEN_NGUOI>"            # mặc định dry-run, incremental
    python -m app.cli process "input/<TEN_NGUOI>" --apply
    python -m app.cli status "input/<TEN_NGUOI>"
    python -m app.cli review-list "input/<TEN_NGUOI>"
    python -m app.cli resolve-review <logical_document_id> --type-id 86 --date 2013-09-10
    python -m app.cli resolve-review <logical_document_id> --type-id 87 --subtype promotion_salary
    python -m app.cli resolve-review <logical_document_id> --supporting
    python -m app.cli resolve-review <logical_document_id> --duplicate-of <logical_document_id_goc>
    python -m app.cli resolve-review <logical_document_id> --date 2023-11 --date-precision MONTH
    python -m app.cli inventory "input/<TEN_NGUOI>"
    python -m app.cli import-state "input/<TEN_NGUOI>"
    python -m app.cli recover-legacy-state "input/<TEN_NGUOI>"
    python -m app.cli state-summary "input/<TEN_NGUOI>"
    python -m app.cli state-export
    python -m app.cli test-golden
    python -m app.cli providers

Mặc định luôn là dry-run: phải gõ rõ --apply mới ghi file.
Mặc định luôn incremental: nguồn đã PROCESSED không bị đọc lại (dùng --no-state để tắt).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .batch import BATCH_SYSTEM_BLOCKED, run_batch
from .catalog import load_catalog
from .fingerprint import current_fingerprint
from .golden import run_all_golden_isolated
from .incremental import DECISION_RETIRED_SOURCE, scan_person_folder
from .models import MODE_APPLY, MODE_DRY_RUN, PipelineError
from .pdf_inventory import PersonInventory, build_inventory, list_pdfs, read_source, sha256_file
from .pipeline import PipelineResult, Workspace, process_person_folder
from .reconcile import reconcile
from .review import list_pending_reviews, resolve_review
from .review_repair import (
    apply_repair, benchmark_fixture, create_repair_plan, decide_finding,
    export_corrections_jsonl, get_repair_plan, list_findings, revision_diff,
    review_history, run_semantic_review, start_review,
)
from .state import SOURCE_ACTIVE, SOURCE_MISSING, SOURCE_RETIRED, StateRegistry
from .state_import import import_person_folder, recover_legacy_person_folder
from .vision_adapter import available_providers

EXIT_OK = 0
EXIT_REVIEW = 0  # REVIEW là kết quả hợp lệ, không phải lỗi
EXIT_BLOCKED = 2
EXIT_ERROR = 3


def _stdout_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def _provider_config(args: argparse.Namespace) -> dict:
    cfg: dict = {}
    if getattr(args, "fixture_root", None):
        cfg["fixture_root"] = args.fixture_root
    if getattr(args, "analysis_root", None):
        cfg["analysis_root"] = args.analysis_root
    return cfg


def _workspace(args: argparse.Namespace) -> Workspace:
    return Workspace(Path(args.root)) if args.root else Workspace.discover()


def print_summary(result: PipelineResult) -> None:
    inv = result.inventory
    print(f"Hồ sơ            : {result.person_folder}")
    print(f"Chế độ           : {result.mode}")
    print(f"Provider         : {json.dumps(result.manifest.get('provider'), ensure_ascii=False)}")
    print(f"File nguồn       : {len(inv.sources)}")
    for s in inv.sources:
        print(f"    - {s.name}  ({s.pages} trang, sha256 {s.sha256[:12]}…)")
    print(f"Tổng số trang    : {inv.total_pages}")

    if result.incremental is not None:
        c = result.incremental.counts()
        print("\n--- Incremental ---")
        print(f"  Mới bổ sung (NEW)          : {c['NEW']}")
        print(f"  Cache hết hạn (STALE)      : {c['STALE_ANALYSIS']}")
        print(f"  Đã phân tích, chờ apply    : {c['CACHED_PENDING_APPLY']}")
        print(f"  Đang chờ review            : {c['CACHED_REVIEW_REQUIRED']}")
        print(f"  Đã hoàn tất trước (SKIP)   : {c['ALREADY_PROCESSED']}")
        print(f"  Lỗi cũ                     : {c['FAILED_PREVIOUSLY']}")
        print(f"  Bị gián đoạn               : {c['INTERRUPTED']}")
        print(f"  Trùng nội dung             : {c['DUPLICATE_SOURCE']}")
        for d in result.incremental.decisions:
            if d.output_mismatch:
                print(f"  CẢNH BÁO STATE_OUTPUT_MISMATCH: {d.source.name}: {d.output_mismatch_detail}")

    summary = result.manifest.get("summary")
    if summary:
        print(f"\nLogical document (toàn hồ sơ) : {summary.get('logical_documents')}")
        print(f"  AUTO_RESOLVED   : {summary.get('auto_resolved')}")
        print(f"  REVIEW_RESOLVED : {summary.get('review_resolved')}")
        print(f"  REVIEW_PENDING  : {summary.get('review_pending')}")

    docs = result.manifest.get("documents") or []
    settled = [d for d in docs if d.get("current_target_filename")]
    pending = [d for d in docs if d.get("needs_review")]
    if settled:
        print("\n--- Đã có tên chính thức (output/) ---")
        for d in sorted(settled, key=lambda x: (x.get("type_id") or "", x.get("sequence_index") or 0)):
            label = d.get("type_id") or d.get("classification_kind") or ""
            print(
                f"  {d['source_file']} trang {d['source_pages']} -> "
                f"[{label}] {d['current_target_filename']} "
                f"({d.get('resolution_status')}{', ngày ' + d['document_date'] if d.get('document_date') else ''})"
            )
    if pending:
        print("\n--- REVIEW_PENDING (cần người quyết định - xem `review-list`) ---")
        for d in sorted(pending, key=lambda x: (x["source_file"], x["source_pages"][0])):
            print(
                f"  [{d['logical_document_id'][:12]}] {d['source_file']} trang {d['source_pages']} "
                f"-> ứng viên [{d['type_id']}] {d.get('title_short') or ''}  lý do: {d.get('review_reason')}"
            )

    for f in result.manifest.get("failed_this_run") or []:
        print(f"\nLỖI (FAILED): {f['source_file']}: {f['error']}")

    print("\n--- QC ---")
    if not result.qc.checks:
        print("  (không có gì mới cần kiểm - không có nguồn nào được xử lý lượt này)")
    for c in result.qc.checks:
        print(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")

    for n in result.notes:
        print(f"\nGhi chú: {n}")
    if result.manifest_path:
        print(f"\nManifest: {result.manifest_path}")
    print(f"\nTRẠNG THÁI: {result.status}")


def cmd_process(args: argparse.Namespace) -> int:
    mode = MODE_APPLY if args.apply else MODE_DRY_RUN
    ws = _workspace(args)
    registry: Optional[StateRegistry] = None
    if not args.no_state:
        registry = StateRegistry(ws.state_db_path)
    try:
        result = process_person_folder(
            Path(args.folder),
            mode=mode,
            provider_name=args.provider,
            provider_config=_provider_config(args),
            workspace=ws,
            force=args.force,
            state_registry=registry,
            retry_review=args.retry_review,
            retry_failed=args.retry_failed,
        )
    finally:
        if registry is not None:
            registry.close()

    if args.json:
        print(json.dumps(result.manifest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_summary(result)
    if result.status in ("BLOCKED_QC", "BLOCKED_RUNTIME", "BLOCKED_MISSING_SOURCE"):
        return EXIT_BLOCKED
    return EXIT_OK


def cmd_batch_run(args: argparse.Namespace) -> int:
    """One-command runtime path for all person folders under input/."""
    ws = _workspace(args)
    report = run_batch(
        Path(args.input_dir),
        workspace=ws,
        provider_name=args.provider,
        provider_config=_provider_config(args),
        apply_enabled=not (args.dry_run or args.no_apply),
        person=args.person,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(report.summary_text())
    return EXIT_BLOCKED if report.status == BATCH_SYSTEM_BLOCKED else EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Chỉ đọc: không mutate registry, không gọi provider, không mở nội dung PDF."""
    ws = _workspace(args)
    folder = Path(args.folder)
    pdf_paths = list_pdfs(folder)
    inv = PersonInventory(folder.name, folder, [read_source(path) for path in pdf_paths])
    output_dir = ws.output / inv.person_folder
    review_dir = ws.review / inv.person_folder
    catalog = load_catalog()
    with StateRegistry(ws.state_db_path) as registry:
        lifecycle = registry.source_lifecycle(inv.person_folder, {s.sha256 for s in inv.sources})
        scan = scan_person_folder(
            inv, registry, mode=MODE_DRY_RUN, fingerprint=current_fingerprint(catalog),
            output_dir=output_dir, review_dir=review_dir,
        )
    if args.json:
        payload = scan.as_dict()
        payload["source_lifecycle"] = [item.as_dict() for item in lifecycle]
        payload["source_lifecycle_counts"] = {
            SOURCE_ACTIVE: sum(item.lifecycle_status == SOURCE_ACTIVE for item in lifecycle),
            SOURCE_MISSING: sum(item.lifecycle_status == SOURCE_MISSING for item in lifecycle),
            SOURCE_RETIRED: sum(item.lifecycle_status == SOURCE_RETIRED for item in lifecycle),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        c = scan.counts()
        print(f"TOTAL: {len(scan.decisions)}")
        print(f"NEW: {c['NEW']}")
        print(f"STALE_ANALYSIS: {c['STALE_ANALYSIS']}")
        print(f"ANALYZED_PENDING_APPLY: {c['CACHED_PENDING_APPLY']}")
        print(f"REVIEW_REQUIRED: {c['CACHED_REVIEW_REQUIRED']}")
        print(f"PROCESSED: {c['ALREADY_PROCESSED']}")
        print(f"FAILED: {c['FAILED_PREVIOUSLY']}")
        if c["INTERRUPTED"]:
            print(f"INTERRUPTED: {c['INTERRUPTED']}")
        if c["DUPLICATE_SOURCE"]:
            print(f"DUPLICATE_SOURCE: {c['DUPLICATE_SOURCE']}")
        if c[DECISION_RETIRED_SOURCE]:
            print(f"RETIRED_SOURCE: {c[DECISION_RETIRED_SOURCE]}")
        for item in lifecycle:
            print(f"{item.lifecycle_status}: {item.source_filename} (sha256 {item.source_hash})")
        for d in scan.decisions:
            if d.output_mismatch:
                print(f"STATE_OUTPUT_MISMATCH: {d.source.name}: {d.output_mismatch_detail}")
    return EXIT_OK


def cmd_review_list(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    person = Path(args.folder).name
    with StateRegistry(ws.state_db_path) as registry:
        items = list_pending_reviews(registry, person)
    if args.json:
        print(json.dumps([i.__dict__ for i in items], ensure_ascii=False, indent=2))
        return EXIT_OK
    if not items:
        print(f"Không có logical document nào đang REVIEW_PENDING cho '{person}'.")
        return EXIT_OK
    for i in items:
        print(f"[{i.logical_document_id}]")
        print(f"  Nguồn        : {i.source_filename} trang {i.source_pages}")
        print(f"  Ứng viên     : {i.type_id} (conf {i.confidence:.2f})")
        print(f"  Ngày (nếu có): {i.document_date}")
        print(f"  Tiêu đề      : {i.title_short}")
        print(f"  Lý do REVIEW : {', '.join(i.classification_reasons)}")
        print()
    print(
        "Chốt bằng: python -m app.cli resolve-review <logical_document_id> "
        "--type-id <mã trong danh mục> [--date yyyy-mm-dd]"
    )
    return EXIT_OK


def cmd_resolve_review(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    catalog = load_catalog()
    with StateRegistry(ws.state_db_path) as registry:
        row = resolve_review(
            registry, catalog, args.logical_document_id,
            type_id=args.type_id, subtype=args.subtype, supporting=args.supporting,
            duplicate_of=args.duplicate_of, document_date=args.date,
            date_precision=args.date_precision, resolved_by=args.by,
        )
    print(
        f"Đã chốt {row.logical_document_id}: kind={row.effective_classification_kind} "
        f"type_id={row.effective_type_id if row.effective_classification_kind == 'TAXONOMY' else '(không áp dụng)'} "
        f"subtype={row.effective_subtype} duplicate_of={row.duplicate_of} "
        f"ngày={row.effective_document_date} (precision={row.effective_date_precision})"
    )
    print("Chạy `process ... --apply` để ghi file thật với tên chính thức.")
    return EXIT_OK


def cmd_reconcile(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    person = Path(args.folder).name
    with StateRegistry(ws.state_db_path) as registry:
        report = reconcile(
            registry, person, ws.output / person, ws.review / person, Path(args.folder)
        )
    print(report.summary_text())
    return EXIT_OK if report.ok else EXIT_BLOCKED


def _review_pages(value: Optional[str]) -> Optional[tuple[int, int]]:
    if not value:
        return None
    try:
        start, end = (int(part) for part in value.split("-", 1))
    except Exception as exc:
        raise PipelineError("--pages phải có dạng 18-24.") from exc
    if start < 1 or end < start:
        raise PipelineError("--pages phải là phạm vi tăng dần, bắt đầu từ 1.")
    return start, end


def _manual_payload(args: argparse.Namespace) -> dict:
    raw = args.payload_file and Path(args.payload_file).read_text(encoding="utf-8") or args.payload
    if not raw:
        raise PipelineError("MANUAL_FIX cần --payload JSON hoặc --payload-file.")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PipelineError("MANUAL_FIX payload phải là JSON hợp lệ.") from exc
    if not isinstance(value, dict):
        raise PipelineError("MANUAL_FIX payload phải là một JSON object.")
    return value


def cmd_review_case(args: argparse.Namespace) -> int:
    ws, folder, catalog = _workspace(args), Path(args.folder), load_catalog()
    person = folder.name
    with StateRegistry(ws.state_db_path) as registry:
        session, findings = start_review(
            registry, person, catalog=catalog, output_dir=ws.output / person, review_dir=ws.review / person,
            source_hash=args.source_hash, pages=_review_pages(args.pages), review_method="deterministic",
        )
    payload = {"session": session.__dict__, "findings": [f.__dict__ for f in findings]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"REVIEW SESSION: {session.session_id} (base revision {session.base_revision})")
        print(f"Findings: {len(findings)} — review không thay đổi canonical state.")
        for item in findings:
            print(f"  [{item.finding_id}] {item.finding_type} trang {item.page_start}-{item.page_end}: {item.reason}")
        print("Duyệt bằng review-approve/review-reject/review-manual-fix, rồi chạy repair-plan.")
    return EXIT_OK


def cmd_review_semantic(args: argparse.Namespace) -> int:
    """Explicit opt-in network/model action; it never applies a repair."""
    from .semantic_reviewer import OpenAICompatibleSemanticReviewer, PdfToPpmRenderer

    ws, folder, catalog = _workspace(args), Path(args.folder), load_catalog()
    reviewer = OpenAICompatibleSemanticReviewer(endpoint=args.endpoint, model=args.model,
        api_key_env=args.api_key_env, timeout_seconds=args.timeout)
    with StateRegistry(ws.state_db_path) as registry:
        session, findings = run_semantic_review(registry, folder.name, catalog=catalog, folder=folder,
            output_dir=ws.output / folder.name, review_dir=ws.review / folder.name, reviewer=reviewer,
            renderer=PdfToPpmRenderer(args.renderer), source_hash=args.source_hash, pages=_review_pages(args.pages))
    payload = {"session": session.__dict__, "findings": [item.__dict__ for item in findings]}
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
          f"SEMANTIC REVIEW SESSION: {session.session_id}; persisted findings: {len(findings)}. Canonical state chưa thay đổi.")
    return EXIT_OK


def cmd_review_findings(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    with StateRegistry(ws.state_db_path) as registry:
        findings = list_findings(registry, args.session_id)
    if args.json:
        print(json.dumps([f.__dict__ for f in findings], ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f"[{f.finding_id}] {f.status} {f.decision or ''} {f.finding_type} pages={f.page_start}-{f.page_end}")
            print(f"  {f.reason}")
    return EXIT_OK


def cmd_review_decision(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    payload = _manual_payload(args) if args.decision == "MANUAL_FIX" else None
    with StateRegistry(ws.state_db_path) as registry:
        finding = decide_finding(registry, args.finding_id, decision=args.decision, reviewer=args.by, manual_fix=payload)
    print(f"{finding.finding_id}: {finding.decision}. Canonical state chưa thay đổi; hãy tạo repair plan.")
    return EXIT_OK


def cmd_repair_plan(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    with StateRegistry(ws.state_db_path) as registry:
        plan = create_repair_plan(registry, args.session_id)
    payload = plan.__dict__
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"REPAIR PLAN: {plan.repair_plan_id}")
        print(f"Base revision: {plan.base_revision}; approved changes: {len(plan.changes)}")
        for change in plan.changes:
            print(f"  {change['finding_type']}: {change['pages']} ({change['decision']})")
        print("Mặc định repair là dry-run. Chỉ dùng repair <plan_id> --apply sau khi đã kiểm tra plan.")
    return EXIT_OK


def cmd_repair(args: argparse.Namespace) -> int:
    ws, folder, catalog = _workspace(args), Path(args.folder), load_catalog()
    with StateRegistry(ws.state_db_path) as registry:
        result = apply_repair(registry, args.repair_plan_id, catalog=catalog, folder=folder,
            output_dir=ws.output / folder.name, review_dir=ws.review / folder.name, dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_review_history(args: argparse.Namespace) -> int:
    ws, person = _workspace(args), Path(args.folder).name
    with StateRegistry(ws.state_db_path) as registry:
        if args.diff:
            try:
                before, after = (int(x) for x in args.diff.split(":", 1))
            except Exception as exc:
                raise PipelineError("--diff phải có dạng 1:2.") from exc
            data = revision_diff(registry, person, before, after)
        else:
            data = review_history(registry, person)
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json or args.diff else "\n".join(
        f"REV {row['revision']} — {row['kind']}: {row['summary']} ({row['created_at']})" for row in data))
    return EXIT_OK


def cmd_review_export_corrections(args: argparse.Namespace) -> int:
    ws, person = _workspace(args), Path(args.folder).name
    with StateRegistry(ws.state_db_path) as registry:
        target = export_corrections_jsonl(registry, person, Path(args.out))
    print(f"Đã export correction ledger: {target}")
    return EXIT_OK


def cmd_review_benchmark_fixture(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    with StateRegistry(ws.state_db_path) as registry:
        fixture = benchmark_fixture(registry, args.finding_id)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Đã tạo anonymized benchmark fixture: {target}")
    return EXIT_OK


def cmd_retire_source(args: argparse.Namespace) -> int:
    """Explicit operator transition for a source whose bytes are absent."""
    folder = Path(args.folder)
    physical_hashes = {sha256_file(path) for path in list_pdfs(folder)}
    ws = _workspace(args)
    with StateRegistry(ws.state_db_path) as registry:
        result = registry.retire_source(
            args.source_hash,
            physical_hashes=physical_hashes,
            reason=args.reason,
            retired_by=args.by,
        )
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"{result.outcome}: {result.source_filename} sha256={result.source_hash} "
            f"previous_status={result.previous_status} retired_at={result.retired_at}"
        )
    return EXIT_OK


def cmd_import_state(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    with StateRegistry(ws.state_db_path) as registry:
        report = import_person_folder(Path(args.folder), registry, workspace=ws)
    print(report.summary_text())
    return EXIT_OK


def cmd_recover_legacy_state(args: argparse.Namespace) -> int:
    """Recovery tường minh, không gọi provider và không tạo/sửa PDF."""
    ws = _workspace(args)
    with StateRegistry(ws.state_db_path) as registry:
        report = recover_legacy_person_folder(Path(args.folder), registry, workspace=ws)
    if args.json:
        print(json.dumps({"person_folder": report.person_folder, "outcomes": [o.__dict__ for o in report.outcomes]}, ensure_ascii=False, indent=2))
    else:
        print(report.summary_text())
    blocked = any(o.outcome == "STATE_IMPORT_REVIEW_REQUIRED" for o in report.outcomes)
    return EXIT_BLOCKED if blocked else EXIT_OK


def cmd_state_summary(args: argparse.Namespace) -> int:
    """Tóm tắt canonical effective state cho báo cáo batch/operator."""
    ws = _workspace(args)
    person = Path(args.folder).name
    current_pdfs = list_pdfs(Path(args.folder))
    with StateRegistry(ws.state_db_path) as registry:
        summary = registry.summarize_person(person, {sha256_file(path) for path in current_pdfs})
    if args.json:
        print(json.dumps({"person_folder": person, **summary}, ensure_ascii=False, indent=2))
    else:
        print(f"STATE SUMMARY: {person}")
        for key in (
            "logical_documents", "active_logical_documents", "historical_logical_documents",
            "active_sources", "missing_sources", "retired_sources", "active_pages",
            "taxonomy", "supporting", "duplicate",
            "auto_resolved", "review_resolved", "review_pending", "historical_review_artifacts",
        ):
            print(f"  {key}: {summary[key]}")
    return EXIT_OK


def cmd_state_export(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    with StateRegistry(ws.state_db_path) as registry:
        data = registry.export_json()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Đã ghi state export: {args.out}")
    else:
        print(text)
    return EXIT_OK


def cmd_inventory(args: argparse.Namespace) -> int:
    inv = build_inventory(Path(args.folder))
    print(json.dumps(inv.as_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_test_golden(args: argparse.Namespace) -> int:
    reports = run_all_golden_isolated(
        Path(args.root) if args.root else None,
        provider_name=args.provider,
        provider_config=_provider_config(args),
    )
    failed = 0
    for r in reports:
        print(r.summary())
        for f in r.failures:
            print(f"   FAIL {f}")
        for t in r.title_diffs:
            print(f"   (thông tin) tiêu đề khác diễn đạt - không tính lỗi: {t}")
        if not r.passed:
            failed += 1
    print()
    if failed:
        print(f"GOLDEN ACCEPTANCE: FAIL ({failed}/{len(reports)} file golden đỏ)")
        return EXIT_BLOCKED
    print(f"GOLDEN ACCEPTANCE: PASS ({len(reports)}/{len(reports)} file golden xanh)")
    return EXIT_OK


def cmd_providers(args: argparse.Namespace) -> int:
    print("Provider đã đăng ký:", ", ".join(available_providers()))
    cat = load_catalog()
    print(f"Catalog: {cat.path} (schema {cat.schema_version}, {len(cat)} loại)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.cli", description="Pipeline số hóa hồ sơ đảng viên")
    p.add_argument("--root", default=None, help="Thư mục gốc repo (mặc định: tự dò)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_provider_args(sp: argparse.ArgumentParser, default_provider: str = "agent") -> None:
        sp.add_argument(
            "--provider",
            default=default_provider,
            help="agent (Antigravity runtime, mặc định) | fixture (chỉ cho test/golden)",
        )
        sp.add_argument("--fixture-root", default=None, help="Thư mục fixture (provider fixture)")
        sp.add_argument(
            "--analysis-root", default=None, help="Thư mục JSON phân tích của Agent (provider agent)"
        )

    sp = sub.add_parser("process", help="Xử lý một thư mục hồ sơ (mặc định incremental)")
    sp.add_argument("folder")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="Mặc định")
    g.add_argument("--apply", action="store_true", help="Thực sự ghi output/review")
    sp.add_argument("--force", action="store_true", help="(dự phòng - đường đi incremental tự fail-safe)")
    sp.add_argument("--json", action="store_true", help="In manifest JSON thay vì summary")
    sp.add_argument(
        "--retry-review", action="store_true",
        help="Đọc lại (Vision) các nguồn đang REVIEW_REQUIRED thay vì dùng cache",
    )
    sp.add_argument(
        "--retry-failed", action="store_true",
        help="Xử lý lại các nguồn FAILED/INTERRUPTED thay vì SKIP mặc định",
    )
    sp.add_argument(
        "--no-state", action="store_true",
        help="Tắt incremental processing - xử lý lại TOÀN BỘ nguồn (không dùng state registry)",
    )
    add_provider_args(sp, default_provider="agent")
    sp.set_defaults(func=cmd_process)

    sp = sub.add_parser(
        "batch-run",
        help="Tự điều phối toàn bộ input/<người>/; chỉ auto-apply hồ sơ AUTO_SAFE",
    )
    sp.add_argument("input_dir", help="Thư mục input/ chứa các thư mục hồ sơ")
    sp.add_argument("--person", default=None, help="Chỉ chạy một tên thư mục hồ sơ")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="Phân tích/validate nhưng không apply")
    g.add_argument("--no-apply", action="store_true", help="Alias của --dry-run")
    sp.add_argument("--json", action="store_true", help="In batch report JSON")
    add_provider_args(sp, default_provider="agent")
    sp.set_defaults(func=cmd_batch_run)

    sp = sub.add_parser("status", help="Xem trạng thái NEW/ANALYZED/REVIEW/PROCESSED/FAILED (chỉ đọc)")
    sp.add_argument("folder")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("review-list", help="Liệt kê logical document đang REVIEW_PENDING của một hồ sơ")
    sp.add_argument("folder")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_review_list)

    sp = sub.add_parser("review-case", help="Rà soát kết quả đã xử lý; chỉ tạo findings, không sửa dữ liệu")
    sp.add_argument("folder", help="input/<TEN_NGUOI>")
    sp.add_argument("--source-hash", default=None, help="Giới hạn một source SHA-256 đã có trong state")
    sp.add_argument("--pages", default=None, help="Giới hạn phạm vi trang, ví dụ 18-24")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_review_case)

    sp = sub.add_parser("review-findings", help="Xem findings của một review session")
    sp.add_argument("session_id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_review_findings)

    sp = sub.add_parser("review-semantic", help="Semantic review có scope rõ ràng; chỉ tạo proposal, không apply")
    sp.add_argument("folder", help="input/<TEN_NGUOI>")
    sp.add_argument("--endpoint", required=True, help="OpenAI-compatible chat-completions endpoint")
    sp.add_argument("--model", required=True)
    sp.add_argument("--api-key-env", default="OPENAI_API_KEY", help="Tên biến môi trường chứa API key")
    sp.add_argument("--timeout", type=int, default=60)
    sp.add_argument("--renderer", default="pdftoppm", help="Đường dẫn/lệnh Poppler pdftoppm")
    sp.add_argument("--source-hash", default=None, help="Giới hạn một source SHA-256")
    sp.add_argument("--pages", default=None, help="Giới hạn phạm vi trang, ví dụ 18-24")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_review_semantic)

    sp = sub.add_parser("review-approve", help="Chấp nhận proposed correction của một finding; chưa apply")
    sp.add_argument("finding_id")
    sp.add_argument("--by", default="operator")
    sp.set_defaults(func=cmd_review_decision, decision="ACCEPT")

    sp = sub.add_parser("review-reject", help="Giữ canonical result hiện có cho một finding")
    sp.add_argument("finding_id")
    sp.add_argument("--by", default="operator")
    sp.set_defaults(func=cmd_review_decision, decision="KEEP_EXISTING")

    sp = sub.add_parser("review-manual-fix", help="Nhập correction JSON có cấu trúc; chưa apply")
    sp.add_argument("finding_id")
    sp.add_argument("--payload", default=None, help="JSON correction; ví dụ {\"type_id\":\"07\"}")
    sp.add_argument("--payload-file", default=None, help="File JSON correction")
    sp.add_argument("--by", default="operator")
    sp.set_defaults(func=cmd_review_decision, decision="MANUAL_FIX")

    sp = sub.add_parser("repair-plan", help="Tạo repair plan từ các finding đã ACCEPT/MANUAL_FIX")
    sp.add_argument("session_id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_repair_plan)

    sp = sub.add_parser("repair", help="Thực thi targeted repair từ plan (mặc định dry-run)")
    sp.add_argument("repair_plan_id")
    sp.add_argument("folder", help="input/<TEN_NGUOI>")
    sp.add_argument("--apply", action="store_true", help="Tạo revision mới và cập nhật artifacts đã duyệt")
    sp.set_defaults(func=cmd_repair)

    sp = sub.add_parser("review-history", help="Xem revision history hoặc diff của một hồ sơ")
    sp.add_argument("folder")
    sp.add_argument("--diff", default=None, help="So sánh revision, ví dụ 1:2")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_review_history)

    sp = sub.add_parser("review-export-corrections", help="Xuất correction ledger JSONL cho audit/benchmark")
    sp.add_argument("folder")
    sp.add_argument("--out", required=True, help="Đường dẫn JSONL đích")
    sp.set_defaults(func=cmd_review_export_corrections)

    sp = sub.add_parser("review-benchmark-fixture", help="Tạo fixture metadata anonymized từ finding đã xác nhận")
    sp.add_argument("finding_id")
    sp.add_argument("--out", required=True, help="Đường dẫn JSON fixture đích (không chứa PDF thật)")
    sp.set_defaults(func=cmd_review_benchmark_fixture)

    sp = sub.add_parser("resolve-review", help="Chốt một logical document REVIEW_PENDING (không đọc lại PDF)")
    sp.add_argument("logical_document_id")
    sp.add_argument(
        "--type-id", default=None, dest="type_id",
        help="type_id đúng trong document_types.json (TAXONOMY - chỉ 1 trong --type-id/--supporting/--duplicate-of)",
    )
    sp.add_argument(
        "--subtype", default=None,
        help="Metadata phụ khi --type-id 87 (transfer/assignment/appointment/"
        "professional_title_appointment/promotion_salary/retirement/other_personnel_decision)",
    )
    sp.add_argument(
        "--supporting", action="store_true",
        help="Chốt là SUPPORTING_DOCUMENT (ngoài danh mục 104 loại, không dùng STT giả)",
    )
    sp.add_argument(
        "--duplicate-of", default=None, dest="duplicate_of",
        help="logical_document_id của bản GỐC - chốt tài liệu này là DUPLICATE (không tạo output riêng)",
    )
    sp.add_argument("--date", default=None, help="document_date yyyy-mm-dd / yyyy-mm / yyyy (bỏ trống nếu không xác định được)")
    sp.add_argument(
        "--date-precision", default=None, dest="date_precision",
        help="DAY/MONTH/YEAR/UNKNOWN - phải khớp --date (UNKNOWN dùng khi --date bỏ trống)",
    )
    sp.add_argument("--by", default="operator", help="Người chốt (ghi vào resolved_by)")
    sp.set_defaults(func=cmd_resolve_review)

    sp = sub.add_parser("reconcile", help="Đối chiếu state DB với file thật trên đĩa (chỉ báo cáo)")
    sp.add_argument("folder")
    sp.set_defaults(func=cmd_reconcile)

    sp = sub.add_parser(
        "retire-source",
        help="Operator xác nhận source đã mất khỏi hồ sơ (cần source SHA-256, không tự động)",
    )
    sp.add_argument("folder", help="input/<TEN_NGUOI>")
    sp.add_argument("source_hash", help="SHA-256 source trong state (không dùng filename)")
    sp.add_argument("--reason", default=None, help="Lý do audit tùy chọn")
    sp.add_argument("--by", default="operator", help="Operator xác nhận")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_retire_source)

    sp = sub.add_parser("inventory", help="Chỉ liệt kê file nguồn + SHA-256")
    sp.add_argument("folder")
    sp.set_defaults(func=cmd_inventory)

    sp = sub.add_parser(
        "import-state",
        help="Nạp lại state PROCESSED từ manifest/output đã có sẵn (migration, xem LIMITATIONS.md)",
    )
    sp.add_argument("folder")
    sp.set_defaults(func=cmd_import_state)

    sp = sub.add_parser(
        "recover-legacy-state",
        help="Hydrate logical_documents từ legacy ledger/artifact đã có (không gọi provider)",
    )
    sp.add_argument("folder")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_recover_legacy_state)

    sp = sub.add_parser(
        "state-summary",
        help="Đếm effective classification + REVIEW_PENDING từ canonical state",
    )
    sp.add_argument("folder")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_state_summary)

    sp = sub.add_parser("state-export", help="Xuất toàn bộ state registry ra JSON (backup/kiểm tra)")
    sp.add_argument("--out", default=None, help="Ghi ra file thay vì in stdout")
    sp.set_defaults(func=cmd_state_export)

    sp = sub.add_parser("test-golden", help="Chạy golden acceptance")
    add_provider_args(sp, default_provider="fixture")
    sp.set_defaults(func=cmd_test_golden)

    sp = sub.add_parser("providers", help="Liệt kê provider/catalog")
    sp.set_defaults(func=cmd_providers)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    _stdout_utf8()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except PipelineError as exc:
        print(f"LỖI PIPELINE: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
