from __future__ import annotations

from datetime import datetime
from typing import Any

STATUS_LABELS: dict[str, str] = {
    "CHO_XAC_MINH": "Chờ xác minh",
    "DU_TAI_LIEU": "Đủ tài liệu",
    "CO_TAI_LIEU": "Có tài liệu",
    "KHONG_PHAT_SINH": "Không phát sinh",
    "CHUA_XAC_DINH": "Chưa xác định",
    "CAN_BO_SUNG": "Cần bổ sung",
    "CHO_KIEM_TRA": "Chờ kiểm tra",
    "DANG_SO_HOA": "Đang số hóa",
    "CHUA_XU_LY": "Chưa xử lý",
    "HOAN_THANH": "Hoàn thành",
    "CAN_XAC_MINH": "Cần xác minh",
    "REVIEW_PENDING": "Chờ rà soát",
    "REVIEW_REQUIRED": "Cần rà soát",
    "PROCESSED": "Đã xử lý",
    "FAILED": "Xử lý lỗi",
    "NEW": "Mới",
    "STALE_ANALYSIS": "Cần phân tích lại",
    "ANALYZED_PENDING_APPLY": "Đã phân tích, chờ áp dụng",
    "ACCEPT": "Chấp nhận",
    "KEEP_EXISTING": "Giữ nguyên",
    "MANUAL_FIX": "Sửa thủ công",
    "APPROVED": "Đã duyệt",
    "REJECTED": "Từ chối",
    "PENDING": "Đang chờ",
    "SUCCESS": "Thành công",
    "ERROR": "Lỗi",
    "RUNNING": "Đang chạy",
}

PARSE_STATUS_LABELS: dict[str, str] = {
    "OK": "Hợp lệ",
    "MALFORMED_NAME": "Tên tệp không chuẩn",
    "FILE_NGOAI_TAXONOMY": "Ngoài danh mục chuẩn",
    "FILE_KHONG_DOC_DUOC": "Không đọc được tệp",
    "FILE_BI_THIEU": "Tệp bị thiếu",
    "ERROR": "Lỗi phân tích",
}

WARNING_TYPE_LABELS: dict[str, str] = {
    "SAI_TEN_THU_MUC": "Sai tên thư mục",
    "SAI_TEN_FILE": "Sai tên tệp",
    "FILE_NGOAI_TAXONOMY": "Ngoài danh mục chuẩn",
    "TRUNG_TAI_LIEU": "Trùng lặp tài liệu",
    "FILE_KHONG_DOC_DUOC": "Không đọc được tệp",
    "CHANGED_AFTER_COMPLETION": "Thay đổi sau khi hoàn thành",
    "REVIEW_PENDING": "Chờ rà soát",
    "CAN_XAC_MINH": "Cần xác minh",
}

EVENT_TYPE_LABELS: dict[str, str] = {
    "AUTO_STATUS_CHANGED": "Tự động cập nhật trạng thái",
    "MANUAL_STATUS_CHANGED": "Chỉnh sửa trạng thái thủ công",
    "CHECKLIST_OVERRIDE": "Điều chỉnh checklist",
    "MARK_COMPLETED": "Đánh dấu hoàn thành",
    "REOPENED": "Mở lại hồ sơ",
    "NOTE_UPDATED": "Cập nhật ghi chú",
    "DOCUMENT_ADDED": "Thêm tài liệu mới",
}

BOOLEAN_LABELS: dict[str, str] = {
    "true": "Có",
    "false": "Không",
    "1": "Có",
    "0": "Không",
    "yes": "Có",
    "no": "Không",
}

ALL_UI_LABELS: dict[str, str] = {
    **STATUS_LABELS,
    **PARSE_STATUS_LABELS,
    **WARNING_TYPE_LABELS,
    **EVENT_TYPE_LABELS,
    **BOOLEAN_LABELS,
}


def ui_label(value: Any, fallback: str | None = None) -> str:
    if value is None:
        return fallback or "Chưa xác định"
    val_str = str(value).strip()
    if not val_str:
        return fallback or ""
    if val_str in ALL_UI_LABELS:
        return ALL_UI_LABELS[val_str]
    lower = val_str.lower()
    if lower in BOOLEAN_LABELS:
        return BOOLEAN_LABELS[lower]
    if val_str.isupper() and "_" in val_str:
        return fallback or "Chưa xác định"
    return val_str


def format_datetime(value: Any, fallback: str = "Chưa quét") -> str:
    if not value:
        return fallback
    val_str = str(value).strip()
    try:
        clean_str = val_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        return dt.strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return val_str


def format_date(value: Any, fallback: str = "—") -> str:
    if not value:
        return fallback
    val_str = str(value).strip()
    try:
        dt = datetime.strptime(val_str[:10], "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return val_str
