"""Golden acceptance - chạy pipeline thật rồi đối chiếu test_cases/*.json.

KHÔNG được sửa golden label để test xanh. Nếu tin rằng label sai, báo riêng cho
người vận hành và chờ duyệt (AGENTS.md mục 2.2).

Golden kiểm 4 trục theo đúng khối `acceptance` trong file golden:
  - page_coverage_required
  - page_overlap_allowed
  - segmentation_exact_match   -> nhóm trang phải trùng tuyệt đối
  - classification_exact_or_expected_review
Thêm một trục chặt hơn do repo tự áp: document_date phải khớp ở những case golden
có ghi rõ trường này.

Lưu ý về `expected_review`: golden đặt trường này cạnh type_id/title_short/
document_date, tức là ở TRỤC PHÂN LOẠI. Vì vậy nó được đối chiếu với
`classification_status`, không phải trạng thái cuối cùng sau naming (một tài
liệu phân loại chắc chắn vẫn có thể sang REVIEW ở Phase E nếu nhóm cùng loại
không xếp được thứ tự thời gian - AGENTS.md Phase E).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .catalog import find_catalog_path
from .golden_fixtures import isolated_golden_workspace
from .models import PipelineError
from .pipeline import MODE_DRY_RUN, PipelineResult, Workspace, process_person_folder

GOLDEN_DIR = "test_cases"


@dataclass
class GoldenFailure:
    case: str
    axis: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.axis}] {self.case}: {self.detail}"


@dataclass
class GoldenReport:
    golden_file: str
    person_folder: str
    failures: list[GoldenFailure] = field(default_factory=list)
    checked_documents: int = 0
    checked_pages: int = 0
    title_diffs: list[str] = field(default_factory=list)
    result: Optional[PipelineResult] = None

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        state = "PASS" if self.passed else "FAIL"
        return (
            f"GOLDEN {state} - {self.golden_file}: "
            f"{self.checked_documents} logical document / {self.checked_pages} trang, "
            f"{len(self.failures)} lỗi"
        )


def list_golden_files(root: Optional[Path] = None) -> list[Path]:
    root = Path(root) if root else find_catalog_path().parent
    d = root / GOLDEN_DIR
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"))


def locate_person_folder(root: Path, source_files: list[str]) -> Path:
    """Tìm thư mục người chứa đúng các file nguồn mà golden nhắc tới."""
    input_root = Path(root) / "input"
    if not input_root.is_dir():
        raise PipelineError(f"Không có thư mục input/: {input_root}")
    wanted = set(source_files)
    candidates = [d for d in sorted(input_root.iterdir()) if d.is_dir()]
    for folder in candidates:
        names = {f.name for f in folder.iterdir() if f.is_file()}
        if wanted <= names:
            return folder
    raise PipelineError(
        f"Không tìm thấy thư mục input/ nào chứa đủ các file golden: {sorted(wanted)}"
    )


def run_golden_file(
    golden_path: Path,
    *,
    root: Optional[Path] = None,
    provider_name: str = "fixture",
    provider_config: Optional[dict] = None,
) -> GoldenReport:
    root = Path(root) if root else find_catalog_path().parent
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    cases = golden.get("cases") or []
    acceptance = golden.get("acceptance") or {}
    source_files = [c["source_file"] for c in cases]
    folder = locate_person_folder(root, source_files)

    result = process_person_folder(
        folder,
        mode=MODE_DRY_RUN,
        provider_name=provider_name,
        provider_config=provider_config,
        workspace=Workspace(root),
        write_manifest=False,
    )
    report = GoldenReport(
        golden_file=Path(golden_path).name, person_folder=folder.name, result=result
    )

    actual_by_source: dict[str, list] = {}
    for d in result.documents:
        actual_by_source.setdefault(d.document.source_file, []).append(d)
    for docs in actual_by_source.values():
        docs.sort(key=lambda d: d.document.source_pages[0])

    for case in cases:
        src = case["source_file"]
        expected_docs = case.get("expected_documents") or []
        actual = actual_by_source.get(src, [])

        # --- trục 0: số trang nguồn khớp mô tả golden ---
        declared_pages = case.get("source_pages")
        try:
            real_pages = result.inventory.by_name(src).pages
        except PipelineError:
            report.failures.append(GoldenFailure(src, "source", "không tìm thấy file nguồn"))
            continue
        if declared_pages is not None and int(declared_pages) != real_pages:
            report.failures.append(
                GoldenFailure(src, "source", f"golden ghi {declared_pages} trang, thực tế {real_pages}")
            )
        report.checked_pages += real_pages

        # --- trục 1: segmentation ---
        exp_pages = [list(d["pages"]) for d in expected_docs]
        act_pages = [list(d.document.source_pages) for d in actual]
        if acceptance.get("segmentation_exact_match", True) and exp_pages != act_pages:
            report.failures.append(
                GoldenFailure(src, "segmentation", f"kỳ vọng {exp_pages}, thực tế {act_pages}")
            )
            continue  # lệch nhóm trang thì không so tiếp từng tài liệu

        # --- trục 2: coverage / overlap trên chính file này ---
        flat = [p for pages in act_pages for p in pages]
        coverage = len(set(flat)) / real_pages if real_pages else 0.0
        required = float(acceptance.get("page_coverage_required", 1.0))
        if coverage < required:
            report.failures.append(
                GoldenFailure(src, "coverage", f"page coverage {coverage:.3f} < {required}")
            )
        overlap = len(flat) - len(set(flat))
        if overlap > int(acceptance.get("page_overlap_allowed", 0)):
            report.failures.append(GoldenFailure(src, "overlap", f"{overlap} trang bị dùng lặp"))

        # --- trục 3: classification + review + date ---
        for exp, act in zip(expected_docs, actual):
            report.checked_documents += 1
            where = f"{src} trang {exp['pages']}"
            if exp["type_id"] != act.classification.type_id:
                report.failures.append(
                    GoldenFailure(
                        src,
                        "classification",
                        f"{where}: kỳ vọng type {exp['type_id']}, thực tế {act.classification.type_id}",
                    )
                )
            exp_review = bool(exp.get("expected_review", False))
            act_review = act.classification_status == "REVIEW"
            if exp_review != act_review:
                report.failures.append(
                    GoldenFailure(
                        src,
                        "review_flag",
                        f"{where}: kỳ vọng expected_review={exp_review}, thực tế "
                        f"classification_status={act.classification_status} "
                        f"({act.classification_reasons or 'không có cờ'})",
                    )
                )
            if "document_date" in exp and exp["document_date"] != act.classification.document_date:
                report.failures.append(
                    GoldenFailure(
                        src,
                        "document_date",
                        f"{where}: kỳ vọng {exp['document_date']!r}, thực tế "
                        f"{act.classification.document_date!r}",
                    )
                )
            exp_title = (exp.get("title_short") or "").strip()
            act_title = (act.classification.title_short or "").strip()
            if exp_title and exp_title.casefold() != act_title.casefold():
                # Tiêu đề do model tự diễn đạt -> chỉ ghi nhận để người vận hành đọc,
                # không tính là lỗi acceptance (golden không đặt trục này).
                report.title_diffs.append(f"{where}: golden={exp_title!r} / model={act_title!r}")

    return report


def run_all_golden(
    root: Optional[Path] = None,
    *,
    provider_name: str = "fixture",
    provider_config: Optional[dict] = None,
) -> list[GoldenReport]:
    root = Path(root) if root else find_catalog_path().parent
    files = list_golden_files(root)
    if not files:
        raise PipelineError(f"Không có file golden nào trong {root / GOLDEN_DIR}")
    return [
        run_golden_file(
            f, root=root, provider_name=provider_name, provider_config=provider_config
        )
        for f in files
    ]


def run_all_golden_isolated(
    repo_root: Optional[Path] = None,
    *,
    provider_name: str = "fixture",
    provider_config: Optional[dict] = None,
    temp_parent: Optional[Path] = None,
) -> list[GoldenReport]:
    """Run Golden in a temporary synthetic workspace, never against ``input/``.

    Provider fixture roots are forced to the staged immutable fixtures.  This
    prevents an accidental CLI override from falling back to production
    ``analysis/`` or another runtime folder.
    """
    repo_root = Path(repo_root) if repo_root else find_catalog_path().parent
    with isolated_golden_workspace(repo_root, temp_parent=temp_parent) as staged:
        config = dict(provider_config or {})
        if provider_name == "agent":
            config["analysis_root"] = str(staged.analysis_root)
        elif provider_name == "fixture":
            config["fixture_root"] = str(staged.fixture_root)
        return run_all_golden(staged.root, provider_name=provider_name, provider_config=config)
