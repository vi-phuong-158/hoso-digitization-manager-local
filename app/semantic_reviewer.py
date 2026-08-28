"""Optional, explicit semantic-review adapters.

This module is deliberately outside the normal pipeline/provider registry.  It
is only invoked by an operator's semantic-review command, renders the selected
source pages transiently, and persists only validated metadata proposals.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from .models import PipelineError

SEMANTIC_REVIEW_PROMPT_VERSION = "review-existing-result.v1"
PROMPT = """You are a reviewer of an existing classification. Do not assume the existing result is correct. Do not rebuild the whole case unless evidence requires it. Look specifically for classification errors, incorrect document boundaries, merge/split errors, missing documents, duplicate mistakes, filename/title inconsistency, and page-order mistakes. Return findings only; do not make changes. If evidence is insufficient, return uncertainty rather than inventing facts. Return a JSON object with a `findings` array. Each finding contains finding_type, source_pages, affected logical-document IDs in existing_result, current and proposed results, reason, confidence, and evidence."""


@dataclass(frozen=True)
class RenderedPage:
    page_number: int
    png_bytes: bytes


@dataclass(frozen=True)
class SemanticReviewRequest:
    source_hash: str
    source_filename: str
    rendered_pages: list[RenderedPage]
    documents: list[dict[str, Any]]
    taxonomy: list[dict[str, Any]]
    reviewer_version: str


class SemanticReviewer(Protocol):
    reviewer_version: str

    def review(self, request: SemanticReviewRequest) -> list[dict[str, Any]]: ...


class PdfToPpmRenderer:
    """Render selected source pages via Poppler, without persisting page images."""
    def __init__(self, executable: str = "pdftoppm") -> None:
        self.executable = executable

    def render(self, source: Path, pages: list[int]) -> list[RenderedPage]:
        if not source.is_file():
            raise PipelineError("Semantic review không tìm thấy PDF nguồn.")
        rendered: list[RenderedPage] = []
        with tempfile.TemporaryDirectory(prefix="hoso-semantic-review-") as temp:
            root = Path(temp)
            for page in pages:
                prefix = root / f"page-{page}"
                try:
                    completed = subprocess.run(
                        [self.executable, "-png", "-r", "120", "-f", str(page), "-l", str(page), str(source), str(prefix)],
                        check=False, capture_output=True, timeout=45,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise PipelineError("Không thể render trang cho semantic review; cần Poppler pdftoppm.") from exc
                image = prefix.with_name(prefix.name + f"-{page}.png")
                if completed.returncode != 0 or not image.is_file():
                    raise PipelineError("Render semantic review thất bại; canonical state không bị thay đổi.")
                rendered.append(RenderedPage(page, image.read_bytes()))
        return rendered


class OpenAICompatibleSemanticReviewer:
    """OpenAI-compatible JSON adapter, used only after explicit operator action."""
    def __init__(self, *, endpoint: str, model: str, api_key_env: str = "OPENAI_API_KEY", timeout_seconds: int = 60) -> None:
        self.endpoint, self.model, self.api_key_env = endpoint, model, api_key_env
        self.timeout_seconds = timeout_seconds
        self.reviewer_version = f"openai-compatible:{model}:{SEMANTIC_REVIEW_PROMPT_VERSION}"

    def review(self, request: SemanticReviewRequest) -> list[dict[str, Any]]:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise PipelineError(f"SEMANTIC_AI_RUNTIME_PENDING: chưa có biến môi trường {self.api_key_env}.")
        content: list[dict[str, Any]] = [{"type": "text", "text": PROMPT + "\n\nContext:\n" + json.dumps({
            "source_hash": request.source_hash, "source_filename": request.source_filename,
            "documents": request.documents, "taxonomy": request.taxonomy,
            "reviewer_version": request.reviewer_version,
        }, ensure_ascii=False)}]
        for page in request.rendered_pages:
            encoded = base64.b64encode(page.png_bytes).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
        payload = json.dumps({"model": self.model, "messages": [{"role": "user", "content": content}], "response_format": {"type": "json_object"}, "temperature": 0}).encode("utf-8")
        req = Request(self.endpoint, data=payload, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:  # nosec B310: explicit operator-configured endpoint
                raw = json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise PipelineError("Semantic reviewer không phản hồi hợp lệ; không tạo finding/canonical mutation.") from exc
        try:
            content_value = raw["choices"][0]["message"]["content"]
            if isinstance(content_value, str) and content_value.startswith("```"):
                content_value = content_value.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(content_value) if isinstance(content_value, str) else content_value
            findings = result["findings"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise PipelineError("Semantic reviewer trả JSON/schema không hợp lệ; đã fail closed.") from exc
        if not isinstance(findings, list):
            raise PipelineError("Semantic reviewer trả findings không phải mảng; đã fail closed.")
        return findings
