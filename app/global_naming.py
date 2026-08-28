"""Global cross-run naming (Phase F/G/H/I của nhiệm vụ incremental).

`app/naming.py` (đánh số trong PHẠM VI MỘT LƯỢT CHẠY) giữ nguyên, không đổi -
vẫn dùng cho đường đi cũ (không state) để không phá 200 test hiện có.

Module này là bản NÂNG CẤP dùng riêng cho đường đi incremental: đánh số
`.1/.2/...` nhìn TOÀN BỘ logical document đã biết của một người + một
`type_id`, bất kể chúng được phân tích ở lượt chạy nào, nguồn PDF nào.

Chính sách khác `app/naming.py` ở một điểm CÓ CHỦ ĐÍCH: hai tài liệu trùng
ngày không còn bị chặn REVIEW nữa mà được xếp bằng tie-break deterministic
(Phase G). Thiếu ngày đáng tin cậy thì vẫn không xếp được -> REVIEW như cũ.
"""
from __future__ import annotations

import uuid
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .catalog import Catalog
from .models import PipelineError
from .naming import DEFAULT_NAMING_POLICY, NamingPolicy, auto_filename
from .policy import (
    DATE_PRECISION_DAY,
    DATE_PRECISION_UNKNOWN,
    date_range,
    ranges_overlap,
    supporting_filename,
)
from .state import LogicalDocumentRow
from .textnorm import normalize
from .writer import split_pages

R_MISSING_RELIABLE_DATE = "ORDERING_MISSING_RELIABLE_DATE"
# DEV POLICY CLOSURE (Policy 4): hai độ chính xác khác nhau mà khoảng ngày
# chồng lấn - không được tự giả định thứ tự (section 11).
R_ORDER_AMBIGUOUS = "ORDER_AMBIGUOUS"
SAME_DATE_TIE_BREAK = "deterministic"  # ghi vào manifest theo yêu cầu Phase G


@dataclass(frozen=True)
class NameableDoc:
    """Chiếu một LogicalDocumentRow xuống đúng phần global naming cần."""

    logical_document_id: str
    source_hash: str
    source_pages: tuple[int, ...]
    document_date: Optional[str]
    date_confidence: float
    title_short: Optional[str]
    # Thêm sau (DEV POLICY CLOSURE) - đặt cuối + có default để không phá vỡ
    # các lời gọi vị trí (positional) đã có trong test cũ.
    date_precision: str = DATE_PRECISION_DAY

    @classmethod
    def from_row(cls, row: LogicalDocumentRow) -> "NameableDoc":
        precision = row.effective_date_precision
        if not precision:
            precision = DATE_PRECISION_DAY if row.effective_document_date else DATE_PRECISION_UNKNOWN
        return cls(
            logical_document_id=row.logical_document_id,
            source_hash=row.source_hash,
            source_pages=tuple(row.source_pages),
            document_date=row.effective_document_date,
            date_confidence=row.date_confidence,
            title_short=row.title_short,
            date_precision=precision,
        )


def _tie_break_key(d: NameableDoc) -> tuple:
    """Ưu tiên: normalized_title -> source_hash -> source_pages (Phase G).

    Thuần dữ liệu đã biết, không phụ thuộc thứ tự file được scan/liệt kê.
    """
    return (normalize(d.title_short) or "", d.source_hash, d.source_pages)


def _sort_key(d: NameableDoc) -> tuple:
    return (d.document_date, _tie_break_key(d))


def orderability_reasons(
    docs: Sequence[NameableDoc], policy: NamingPolicy = DEFAULT_NAMING_POLICY
) -> list[str]:
    """Rỗng <=> xếp được thứ tự toàn cục an toàn.

    Hai lý do KHÔNG xếp được:
      - thiếu ngày đáng tin cậy (như cũ);
      - (Policy 4) hai tài liệu có khoảng ngày (theo precision DAY/MONTH/YEAR)
        CHỒNG LẤN nhưng KHÔNG bằng nhau hệt - không biết ai trước ai sau, ví
        dụ MONTH "2023-11" và DAY "2023-11-05". Khoảng bằng NHAU HỆT (cùng giá
        trị, cùng precision) không phải ambiguous - đó là ca "trùng ngày",
        xếp bằng tie-break xác định (Phase G), không cần REVIEW.
    """
    reasons: list[str] = []
    dated: list[tuple[NameableDoc, tuple]] = []
    for d in docs:
        if not d.document_date or d.date_confidence < policy.min_date_confidence:
            if R_MISSING_RELIABLE_DATE not in reasons:
                reasons.append(R_MISSING_RELIABLE_DATE)
            continue
        rng = date_range(d.document_date, d.date_precision)
        if rng is None:
            if R_MISSING_RELIABLE_DATE not in reasons:
                reasons.append(R_MISSING_RELIABLE_DATE)
            continue
        dated.append((d, rng))
    for i in range(len(dated)):
        _, r1 = dated[i]
        for j in range(i + 1, len(dated)):
            _, r2 = dated[j]
            if r1 == r2:
                continue
            if ranges_overlap(r1, r2) and R_ORDER_AMBIGUOUS not in reasons:
                reasons.append(R_ORDER_AMBIGUOUS)
    return reasons


@dataclass(frozen=True)
class GlobalAssignment:
    logical_document_id: str
    sequence_index: Optional[int]
    target_filename: str


def compute_global_assignment(
    catalog: Catalog,
    type_id: str,
    docs: Sequence[NameableDoc],
    policy: NamingPolicy = DEFAULT_NAMING_POLICY,
) -> tuple[list[GlobalAssignment], list[str]]:
    """(assignment, reasons). reasons rỗng <=> xếp được thứ tự toàn cục.

    Deterministic tuyệt đối: cùng tập `docs` (bất kể thứ tự truyền vào) luôn
    cho ra cùng kết quả - test bắt buộc phải xác nhận điều này (Phase P #14).
    """
    if not docs:
        return [], []
    reasons = orderability_reasons(docs, policy)
    if reasons:
        return [], reasons
    ordered = sorted(docs, key=_sort_key)
    if len(ordered) == 1:
        only = ordered[0]
        return [GlobalAssignment(only.logical_document_id, None, auto_filename(catalog, type_id))], []
    out = [
        GlobalAssignment(d.logical_document_id, idx, auto_filename(catalog, type_id, idx))
        for idx, d in enumerate(ordered, start=1)
    ]
    return out, []


def compute_supporting_assignment(docs: Sequence[NameableDoc]) -> list[GlobalAssignment]:
    """Đánh số cho SUPPORTING_DOCUMENT cùng tiêu đề (Policy 2/5).

    KHÁC với TAXONOMY: không yêu cầu ngày (nhiều tài liệu ngoài danh mục
    không có ngày đáng tin cậy) - luôn xếp được bằng tie-break xác định
    (title chuẩn hoá -> source_hash -> source_pages), không phải thứ tự thời
    gian. Gọi riêng cho từng nhóm cùng `supporting_group_key` (cùng tiêu đề
    chuẩn hoá) - nhóm khác tiêu đề không cạnh tranh số thứ tự với nhau."""
    if not docs:
        return []
    ordered = sorted(docs, key=_tie_break_key)
    if len(ordered) == 1:
        only = ordered[0]
        return [GlobalAssignment(only.logical_document_id, None, supporting_filename(only.title_short))]
    return [
        GlobalAssignment(d.logical_document_id, idx, supporting_filename(d.title_short, idx))
        for idx, d in enumerate(ordered, start=1)
    ]


# ---------------------------------------------------------------------------
# Rename plan (Phase I) - thuần dữ liệu, không chạm filesystem.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RenameOp:
    logical_document_id: str
    kind: str  # "CREATE" | "MOVE"
    old_filename: Optional[str]
    old_dir: Optional[str]  # "output" | "review"
    new_filename: str
    new_dir: str  # luôn "output" - tài liệu đã settle thì có tên chính thức


def build_rename_plan(
    current: dict[str, tuple[str, str]],  # logical_document_id -> (filename, dir)
    assignment: Sequence[GlobalAssignment],
    new_dir: str = "output",
) -> list[RenameOp]:
    ops: list[RenameOp] = []
    for a in assignment:
        prev = current.get(a.logical_document_id)
        if prev is None:
            ops.append(
                RenameOp(a.logical_document_id, "CREATE", None, None, a.target_filename, new_dir)
            )
            continue
        old_filename, old_dir = prev
        if old_filename == a.target_filename and old_dir == new_dir:
            continue  # không đổi gì - không phải một rename op
        ops.append(
            RenameOp(a.logical_document_id, "MOVE", old_filename, old_dir, a.target_filename, new_dir)
        )
    return ops


def has_collisions(ops: Sequence[RenameOp]) -> list[str]:
    """Bất biến: không hai op nào cùng ghi ra một target_filename."""
    seen: dict[str, str] = {}
    problems = []
    for op in ops:
        key = f"{op.new_dir}/{op.new_filename}"
        if key in seen and seen[key] != op.logical_document_id:
            problems.append(f"Trùng đích {key}: {seen[key]} và {op.logical_document_id}")
        seen[key] = op.logical_document_id
    return problems


# ---------------------------------------------------------------------------
# Thực thi trên filesystem - fail-safe 2 pha (Phase I).
# ---------------------------------------------------------------------------
def execute_rename_plan(
    output_dir: Path,
    review_dir: Path,
    ops: Sequence[RenameOp],
    *,
    source_path_of: dict,  # logical_document_id -> Path (PDF nguồn, chỉ cần cho CREATE)
    pages_of: dict,  # logical_document_id -> list[int]
) -> None:
    """Thực thi toàn bộ `ops`, hoặc không đổi gì cả nếu có lỗi (rollback).

    Không sửa PDF nguồn — CREATE chỉ đọc nguồn để tách trang (writer.split_pages,
    page object, không rasterize). MOVE chỉ đổi tên file trong output/review.
    """

    def _dir(name: str) -> Path:
        return output_dir if name == "output" else review_dir

    moves = [op for op in ops if op.kind == "MOVE"]
    creates = [op for op in ops if op.kind == "CREATE"]

    for op in moves:
        src = _dir(op.old_dir) / op.old_filename
        if not src.is_file():
            raise PipelineError(
                f"Rename plan thất bại: thiếu file nguồn đổi tên '{src}' "
                f"(logical_document_id={op.logical_document_id}). Không đổi gì cả."
            )

    tmp_dir = output_dir / ".rename_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Ngăn xếp các hành động ĐÃ LÀM, mỗi phần tử là một hàm undo - rollback
    # bằng cách gọi ngược LIFO nên luôn đưa được filesystem về trạng thái ban đầu
    # dù lỗi xảy ra ở bất kỳ bước nào.
    undo_stack: list = []
    try:
        # Pha 1: đưa mọi file MOVE về tên tạm - tránh đụng độ khi hoán vị (A<->B).
        tmp_of: dict[str, Path] = {}
        for op in moves:
            src = _dir(op.old_dir) / op.old_filename
            tmp = tmp_dir / f"{uuid.uuid4().hex}.pdf"
            src.rename(tmp)
            tmp_of[op.logical_document_id] = tmp
            # Recovery deliberately uses ``os.replace`` rather than the
            # ordinary rename path.  A failure while finalizing a move must
            # still be able to return staged bytes to their canonical name.
            undo_stack.append(lambda t=tmp, s=src: os.replace(t, s))

        # Pha 2a: từ tên tạm -> tên đích cuối cùng.
        for op in moves:
            dest = _dir(op.new_dir) / op.new_filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = tmp_of[op.logical_document_id]
            tmp.rename(dest)
            undo_stack.append(lambda d=dest, t=tmp: os.replace(d, t))

        # Pha 2b: tài liệu hoàn toàn mới - tách trang trực tiếp ra tên đích.
        for op in creates:
            dest = _dir(op.new_dir) / op.new_filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            split_pages(source_path_of[op.logical_document_id], pages_of[op.logical_document_id], dest)
            undo_stack.append(lambda d=dest: d.unlink(missing_ok=True))
    except Exception as exc:
        for undo in reversed(undo_stack):
            try:
                undo()
            except OSError:
                pass
        raise PipelineError(f"Rename plan thất bại giữa chừng, đã rollback: {exc}") from exc
    finally:
        try:
            tmp_dir.rmdir()
        except OSError:
            pass  # còn file tạm (case lỗi) hoặc thư mục dùng chung - không sao
