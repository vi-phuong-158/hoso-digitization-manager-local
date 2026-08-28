"""Ranh giới model/provider.

Toàn bộ business logic (segmenter, classifier, naming, writer, qc) chỉ nói
chuyện với `DocumentVisionProvider`. Muốn đổi sang Gemini/Claude/model khác chỉ
cần thêm một adapter và đăng ký ở đây - KHÔNG sửa business logic.

Quy tắc:
- Provider chỉ được trả `type_id` có trong catalog hoặc "UNKNOWN".
- Provider KHÔNG được đề xuất tên file (naming là deterministic engine).
- Provider KHÔNG được trả về cách gom nhóm trang (segmentation là của app).
- Output của provider luôn đi qua `validate_page_observation` /
  `validate_classification` trước khi vào pipeline.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .catalog import Catalog
from .models import (
    UNKNOWN,
    DocumentClassification,
    PageObservation,
    PipelineError,
    TypeCandidate,
)

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_ROLES = ("CONTENT", "COVER", "BACK_SIDE", "CONTINUATION", "BLANK")
VALID_HINTS = ("PREVIOUS", "NEXT", "NONE", "UNCERTAIN")


class ProviderError(PipelineError):
    """Provider trả về dữ liệu không hợp lệ hoặc không gọi được."""


class DocumentVisionProvider(ABC):
    """Hợp đồng bắt buộc cho mọi model/provider."""

    name: str = "abstract"

    @abstractmethod
    def analyze_pages(self, pdf_path: Path, page_numbers: Sequence[int]) -> list[PageObservation]:
        """Phase B - tín hiệu cấp trang cho từng trang trong `page_numbers`."""

    @abstractmethod
    def classify_document(
        self,
        pdf_path: Path,
        page_numbers: Sequence[int],
        candidates: Sequence[TypeCandidate],
        *,
        second_pass: bool = False,
        taxonomy: Optional[list[dict]] = None,
    ) -> DocumentClassification:
        """Phase D - phân loại TOÀN BỘ logical document (không chỉ trang đầu)."""

    def proposed_documents(self, pdf_path: Path) -> Optional[list[list[int]]]:
        """Cách gom trang do Agent đề xuất, nếu có.

        Đây CHỈ là đề xuất để đối chiếu chéo. Pipeline vẫn tự chạy segmenter
        deterministic của mình; nếu hai bên lệch nhau thì tài liệu liên quan bị
        đưa REVIEW (không bên nào được coi là đúng mặc định).
        """
        return None

    # Tuỳ chọn: provider có thể khai báo giới hạn/chi phí cho runtime log.
    def describe(self) -> dict[str, Any]:
        return {"provider": self.name}


# --------------------------------------------------------------------------
# Validation - hàng rào giữa model và business logic
# --------------------------------------------------------------------------
def _check_confidence(value: Any, where: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"{where}: confidence không phải số: {value!r}") from exc
    if not (0.0 <= f <= 1.0):
        raise ProviderError(f"{where}: confidence ngoài [0,1]: {f}")
    return f


def _check_date(value: Any, where: str) -> Optional[str]:
    if value in (None, "", "null"):
        return None
    if not isinstance(value, str) or not ISO_DATE_RE.match(value):
        raise ProviderError(f"{where}: document_date phải dạng yyyy-mm-dd, nhận {value!r}")
    return value


def validate_page_observation(obs: PageObservation, catalog: Catalog, where: str) -> PageObservation:
    if obs.page_role not in VALID_ROLES:
        raise ProviderError(f"{where}: page_role không hợp lệ: {obs.page_role!r}")
    if obs.attach_hint not in VALID_HINTS:
        raise ProviderError(f"{where}: attach_hint không hợp lệ: {obs.attach_hint!r}")
    obs.date_confidence = _check_confidence(obs.date_confidence, where)
    obs.attach_hint_confidence = _check_confidence(obs.attach_hint_confidence, where)
    obs.document_date = _check_date(obs.document_date, where)
    for cand in obs.type_candidates:
        if not catalog.is_valid_classification(cand.type_id):
            raise ProviderError(
                f"{where}: type_id '{cand.type_id}' không có trong catalog "
                f"(chỉ chấp nhận 01-104 hoặc {UNKNOWN})."
            )
        _check_confidence(cand.confidence, where)
    return obs


def validate_classification(
    result: DocumentClassification, catalog: Catalog, where: str
) -> DocumentClassification:
    if not catalog.is_valid_classification(result.type_id):
        raise ProviderError(
            f"{where}: type_id '{result.type_id}' không có trong catalog "
            f"(chỉ chấp nhận 01-104 hoặc {UNKNOWN})."
        )
    result.confidence = _check_confidence(result.confidence, where)
    result.date_confidence = _check_confidence(result.date_confidence, where)
    result.document_date = _check_date(result.document_date, where)
    if result.title_short is not None and len(result.title_short) > 200:
        # Không cho model nhét toàn văn tài liệu vào manifest.
        raise ProviderError(f"{where}: title_short quá dài ({len(result.title_short)} ký tự).")
    if result.runner_up is not None:
        if not catalog.is_valid_classification(result.runner_up.type_id):
            raise ProviderError(f"{where}: runner_up.type_id không hợp lệ.")
        _check_confidence(result.runner_up.confidence, where)
    return result


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
ProviderFactory = Callable[[dict], DocumentVisionProvider]
_REGISTRY: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    _REGISTRY[name] = factory


def available_providers() -> list[str]:
    _ensure_builtins()
    return sorted(_REGISTRY)


def get_provider(name: str, config: Optional[dict] = None) -> DocumentVisionProvider:
    _ensure_builtins()
    try:
        factory = _REGISTRY[name]
    except KeyError as exc:
        raise ProviderError(
            f"Provider '{name}' chưa đăng ký. Có sẵn: {sorted(_REGISTRY)}"
        ) from exc
    return factory(config or {})


_builtins_loaded = False


def _ensure_builtins() -> None:
    """Nạp các provider của RUNTIME.

    Runtime là Antigravity-native: KHÔNG có provider nào gọi API AI qua mạng.
    Adapter Gemini cũ nằm ở app/providers/gemini_provider.py, được đánh dấu
    NOT_USED_IN_ANTIGRAVITY_RUNTIME và KHÔNG được import ở đây.
    """
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    from .providers import agent_provider, fixture_provider  # noqa: F401  (self-registering)
