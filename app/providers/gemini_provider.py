"""NOT_USED_IN_ANTIGRAVITY_RUNTIME - adapter Gemini API (đã ngưng dùng).

===========================================================================
 KHÔNG NẰM TRONG RUNTIME PATH.
 Runtime chính thức là Antigravity-native: `app/providers/agent_provider.py`.
 Module này KHÔNG tự đăng ký vào registry và KHÔNG được `app/vision_adapter.py`
 import. Nó chỉ còn để tham khảo và để test giữ lại phần parser/validator.
 Muốn dùng phải gọi tay `register_for_tests()` - và khi đó vẫn phải bật
 `allow_network=True` một cách tường minh.
===========================================================================

Thiết kế: business logic không biết gì về Gemini. Muốn đổi model/provider chỉ
cần thay adapter này hoặc thêm adapter khác. Adapter tuyệt đối không:
  - tự đặt tên file,
  - tự gom nhóm trang thành tài liệu,
  - tự tạo type_id ngoài catalog.

An toàn dữ liệu: chỉ gửi đúng các trang cần thiết, cắt bằng page object
(không rasterize), và chỉ gửi khi `allow_network=True` được bật rõ ràng.
"""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from pypdf import PdfReader, PdfWriter

from ..catalog import Catalog, load_catalog
from ..models import DocumentClassification, PageObservation, TypeCandidate, UNKNOWN
from ..vision_adapter import (
    DocumentVisionProvider,
    ProviderError,
    register_provider,
    validate_classification,
    validate_page_observation,
)

DEFAULT_MODEL = "gemini-2.5-pro"
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# transport(prompt: str, pdf_bytes: bytes, model: str) -> str (văn bản JSON)
Transport = Callable[[str, bytes, str], str]


# --------------------------------------------------------------------------
# Prompt builders (thuần - có unit test)
# --------------------------------------------------------------------------
def catalog_lines(catalog: Catalog) -> str:
    return "\n".join(f"{t.id}: {t.name_vi}" for t in catalog.all_types())


PAGE_ANALYSIS_RULES = """Bạn là bộ đọc trang tài liệu lưu trữ tiếng Việt (hồ sơ đảng viên).
Bạn chỉ mô tả TỪNG TRANG. Bạn KHÔNG được gom trang thành tài liệu, KHÔNG được đặt tên file.

Với mỗi trang được đánh số, trả về đúng các trường sau:
- page_number: số trang đã cho (1-based, theo đúng danh sách đầu vào)
- page_role: "CONTENT" (trang nội dung chính) | "COVER" (bìa ngoài của bằng/chứng chỉ)
             | "BACK_SIDE" (mặt sau của chính trang trước) | "CONTINUATION" (trang tiếp của văn bản trước)
             | "BLANK" (gần như trắng)
- title_guess: tiêu đề/loại văn bản đọc được, ngắn gọn, tối đa 120 ký tự. Không chép toàn văn.
- document_date: ngày ban hành dạng yyyy-mm-dd, hoặc null nếu không thấy
- date_confidence: 0..1
- type_candidates: tối đa 3 mục {"type_id": "<mã trong danh mục hoặc UNKNOWN>", "confidence": 0..1}
- starts_new_document: true nếu trang này bắt đầu một văn bản độc lập
- continues_previous: true nếu trang này là phần tiếp của trang liền trước
- attach_hint: "PREVIOUS" | "NEXT" | "NONE" | "UNCERTAIN" - với trang bìa/mặt sau/trắng,
  cho biết nó thuộc về trang liền trước hay liền sau; nếu không chắc thì "UNCERTAIN"
- attach_hint_confidence: 0..1
- notes: ghi chú rất ngắn hoặc null

Ràng buộc cứng:
- type_id chỉ được lấy trong danh mục dưới đây, hoặc "UNKNOWN". Tuyệt đối không bịa mã mới.
- Không chắc thì hạ confidence, không đoán bừa.
- Chỉ trả về JSON: {"pages": [ ... ]}. Không giải thích thêm."""

CLASSIFY_RULES = """Bạn phân loại MỘT tài liệu logic (gồm toàn bộ các trang được cung cấp,
đã bao gồm bìa/mặt sau nếu có). Hãy đọc tất cả các trang, không chỉ trang đầu.

Trả về đúng JSON:
{"type_id": "<mã trong danh mục hoặc UNKNOWN>", "confidence": 0..1,
 "document_date": "yyyy-mm-dd" hoặc null, "date_confidence": 0..1,
 "title_short": "<tiêu đề ngắn, tối đa 120 ký tự>",
 "runner_up": {"type_id": "...", "confidence": 0..1} hoặc null}

Ràng buộc cứng:
- Không đặt tên file. Không đề xuất số thứ tự.
- type_id chỉ trong danh mục hoặc "UNKNOWN".
- Nếu tài liệu không khớp rõ mô tả loại nào, để confidence thấp thay vì ép nhãn.
- Không chép toàn văn tài liệu vào JSON."""

SECOND_PASS_RULES = """Đây là lượt rà lại độc lập. Kết luận lượt trước CHỈ là tham khảo,
không phải sự thật. Hãy tự đọc lại tài liệu và các mô tả loại dễ nhầm bên dưới rồi kết luận."""


def build_page_analysis_prompt(catalog: Catalog, page_numbers: Sequence[int]) -> str:
    return (
        f"{PAGE_ANALYSIS_RULES}\n\n"
        f"Các trang trong file PDF đính kèm tương ứng số trang gốc: {list(page_numbers)}\n\n"
        f"DANH MỤC LOẠI TÀI LIỆU (nguồn chân lý duy nhất):\n{catalog_lines(catalog)}\n"
    )


def build_classification_prompt(
    catalog: Catalog,
    page_numbers: Sequence[int],
    candidates: Sequence[TypeCandidate],
    *,
    second_pass: bool = False,
    taxonomy: Optional[list[dict]] = None,
) -> str:
    parts = [CLASSIFY_RULES]
    if second_pass:
        parts.append(SECOND_PASS_RULES)
    parts.append(f"Tài liệu gồm các trang gốc: {list(page_numbers)}")
    if candidates:
        cand_txt = ", ".join(f"{c.type_id}({c.confidence:.2f})" for c in candidates[:5])
        parts.append(f"Ứng viên sơ bộ từ bước đọc trang: {cand_txt}")
    focus = taxonomy or catalog.describe([c.type_id for c in candidates[:5]] or [UNKNOWN])
    parts.append("MÔ TẢ CÁC LOẠI CẦN CÂN NHẮC KỸ:\n" + json.dumps(focus, ensure_ascii=False, indent=1))
    parts.append("DANH MỤC ĐẦY ĐỦ:\n" + catalog_lines(catalog))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Response parsers (thuần - có unit test)
# --------------------------------------------------------------------------
def extract_json(text: str) -> Any:
    if text is None:
        raise ProviderError("Model không trả về nội dung.")
    candidate = text.strip()
    block = JSON_BLOCK_RE.search(candidate)
    if block:
        candidate = block.group(1).strip()
    else:
        start = min([i for i in (candidate.find("{"), candidate.find("[")) if i >= 0], default=-1)
        if start > 0:
            candidate = candidate[start:]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Model trả về JSON không hợp lệ: {exc}") from exc


def parse_page_analysis(text: str, page_numbers: Sequence[int], catalog: Catalog) -> list[PageObservation]:
    data = extract_json(text)
    rows = data.get("pages") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ProviderError("Kết quả analyze_pages phải có mảng 'pages'.")
    by_page: dict[int, PageObservation] = {}
    for row in rows:
        obs = PageObservation(
            page_number=int(row["page_number"]),
            page_role=str(row.get("page_role", "CONTENT")).upper(),
            title_guess=row.get("title_guess"),
            document_date=row.get("document_date"),
            date_confidence=float(row.get("date_confidence") or 0.0),
            type_candidates=[
                TypeCandidate(str(c["type_id"]), float(c["confidence"]))
                for c in (row.get("type_candidates") or [])
            ],
            starts_new_document=bool(row.get("starts_new_document", False)),
            continues_previous=bool(row.get("continues_previous", False)),
            attach_hint=str(row.get("attach_hint", "NONE")).upper(),
            attach_hint_confidence=float(row.get("attach_hint_confidence") or 0.0),
            notes=(row.get("notes") or None),
        )
        if obs.title_guess and len(obs.title_guess) > 200:
            obs.title_guess = obs.title_guess[:200]
        validate_page_observation(obs, catalog, where=f"analyze_pages/trang {obs.page_number}")
        by_page[obs.page_number] = obs
    missing = [p for p in page_numbers if p not in by_page]
    if missing:
        raise ProviderError(f"Model bỏ sót trang: {missing}")
    return [by_page[p] for p in page_numbers]


def parse_classification(text: str, catalog: Catalog) -> DocumentClassification:
    data = extract_json(text)
    if not isinstance(data, dict):
        raise ProviderError("Kết quả classify_document phải là object JSON.")
    runner = data.get("runner_up")
    result = DocumentClassification(
        type_id=str(data.get("type_id") or UNKNOWN),
        confidence=float(data.get("confidence") or 0.0),
        document_date=data.get("document_date"),
        date_confidence=float(data.get("date_confidence") or 0.0),
        title_short=(data.get("title_short") or None),
        runner_up=(
            TypeCandidate(str(runner["type_id"]), float(runner["confidence"]))
            if isinstance(runner, dict) and runner.get("type_id")
            else None
        ),
        provider_note="gemini",
    )
    return validate_classification(result, catalog, where="classify_document")


# --------------------------------------------------------------------------
# Cắt trang bằng page object (KHÔNG rasterize)
# --------------------------------------------------------------------------
def extract_pages_bytes(pdf_path: Path, page_numbers: Sequence[int]) -> bytes:
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for p in page_numbers:
        if not (1 <= p <= len(reader.pages)):
            raise ProviderError(f"Trang {p} nằm ngoài '{Path(pdf_path).name}'.")
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------
class GeminiVisionProvider(DocumentVisionProvider):
    name = "gemini"

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.model = config.get("model") or os.environ.get("HSDV_GEMINI_MODEL") or DEFAULT_MODEL
        self.api_key_env = config.get("api_key_env", "GEMINI_API_KEY")
        self.allow_network = bool(config.get("allow_network", False))
        self.max_pages_per_call = int(config.get("max_pages_per_call", 10))
        self._transport: Optional[Transport] = config.get("transport")
        self._catalog: Catalog = config.get("catalog") or load_catalog()

    # -- transport --
    def _ensure_transport(self) -> Transport:
        if self._transport is not None:
            return self._transport
        if not self.allow_network:
            raise ProviderError(
                "Provider 'gemini' đang ở chế độ chặn mạng. Bật bằng --allow-network "
                "(và đặt biến môi trường "
                f"{self.api_key_env}) khi người vận hành đã cho phép gửi tài liệu tới Gemini."
            )
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ProviderError(f"Thiếu biến môi trường {self.api_key_env}.")
        try:  # pragma: no cover - phụ thuộc môi trường runtime
            from google import genai
            from google.genai import types as genai_types
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "Chưa cài SDK 'google-genai'. Cài: pip install google-genai"
            ) from exc

        client = genai.Client(api_key=api_key)

        def _transport(prompt: str, pdf_bytes: bytes, model: str) -> str:  # pragma: no cover
            response = client.models.generate_content(
                model=model,
                contents=[
                    genai_types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                    prompt,
                ],
                config=genai_types.GenerateContentConfig(
                    temperature=0.0, response_mime_type="application/json"
                ),
            )
            return response.text

        self._transport = _transport
        return _transport

    # -- interface --
    def analyze_pages(self, pdf_path: Path, page_numbers: Sequence[int]) -> list[PageObservation]:
        pdf_path = Path(pdf_path)
        page_numbers = list(page_numbers)
        transport = self._ensure_transport()
        out: list[PageObservation] = []
        for start in range(0, len(page_numbers), self.max_pages_per_call):
            chunk = page_numbers[start : start + self.max_pages_per_call]
            prompt = build_page_analysis_prompt(self._catalog, chunk)
            text = transport(prompt, extract_pages_bytes(pdf_path, chunk), self.model)
            out.extend(parse_page_analysis(text, chunk, self._catalog))
        return out

    def classify_document(
        self,
        pdf_path: Path,
        page_numbers: Sequence[int],
        candidates: Sequence[TypeCandidate],
        *,
        second_pass: bool = False,
        taxonomy: Optional[list[dict]] = None,
    ) -> DocumentClassification:
        transport = self._ensure_transport()
        prompt = build_classification_prompt(
            self._catalog, page_numbers, candidates, second_pass=second_pass, taxonomy=taxonomy
        )
        text = transport(prompt, extract_pages_bytes(Path(pdf_path), list(page_numbers)), self.model)
        return parse_classification(text, self._catalog)

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "model": self.model,
            "allow_network": self.allow_network,
            "api_key_env": self.api_key_env,
        }


NOT_USED_IN_ANTIGRAVITY_RUNTIME = True


def register_for_tests() -> None:
    """Đăng ký thủ công. CHỈ dùng trong test; runtime không bao giờ gọi hàm này."""
    register_provider("gemini", lambda config: GeminiVisionProvider(config))
