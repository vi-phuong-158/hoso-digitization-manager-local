"""Chuẩn hóa chuỗi tiếng Việt (bỏ dấu) và đo tương đồng tiêu đề.

Dùng cho: so khớp tiêu đề bìa với tiêu đề trang nội dung, và sinh phần hậu tố
truy vết cho file REVIEW. KHÔNG dùng để sinh tên file chính thức
(tên file chính thức chỉ lấy filename_base từ document_types.json).
"""
from __future__ import annotations

import re
import unicodedata

_D_MAP = str.maketrans({"đ": "d", "Đ": "D"})
_NON_WORD = re.compile(r"[^a-z0-9]+")

# Từ quá phổ biến trong văn bản hành chính VN -> không mang thông tin phân biệt.
STOPWORDS = frozenset(
    """cong hoa xa hoi chu nghia viet nam doc lap tu do hanh phuc va cua cho
    ban sao so nam thang ngay cap trinh do khoa hoi dong""".split()
)


def strip_accents(text: str) -> str:
    text = text.translate(_D_MAP)
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str | None) -> str:
    if not text:
        return ""
    return _NON_WORD.sub(" ", strip_accents(text).lower()).strip()


def tokens(text: str | None, drop_stopwords: bool = True) -> set[str]:
    words = [w for w in normalize(text).split() if len(w) > 1]
    if drop_stopwords:
        words = [w for w in words if w not in STOPWORDS]
    return set(words)


def title_similarity(a: str | None, b: str | None) -> float:
    """Độ tương đồng 0..1 giữa hai tiêu đề, theo overlap có trọng tâm.

    Bìa thường chỉ in tiêu đề ngắn ("CHỨNG CHỈ TIN HỌC ỨNG DỤNG") trong khi
    trang nội dung dài hơn, nên dùng overlap-coefficient (chia cho tập nhỏ hơn)
    thay vì Jaccard để không phạt oan trang bìa.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if inter == 0:
        return 0.0
    return inter / min(len(ta), len(tb))


def slug(text: str | None, max_len: int = 40) -> str:
    s = normalize(text).replace(" ", "_")
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len].strip("_")
