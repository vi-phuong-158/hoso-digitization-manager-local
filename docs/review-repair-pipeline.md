# Review & Repair Pipeline

## Mục đích

Review & Repair rà soát kết quả pipeline đã có mà không đọc lại toàn bộ corpus,
không sửa PDF nguồn và không cho AI tự thay đổi canonical state.

```text
Processed state
   -> deterministic audit
   -> optional semantic findings
   -> human decision
   -> repair plan (dry-run)
   -> targeted repair
   -> new revision + correction ledger
   -> output/state validation
```

## Dữ liệu và an toàn

- Các bảng `review_sessions`, `review_findings`, `repair_plans`,
  `case_revisions`, `correction_ledger` nằm trong `state/processing_state.db`
  (schema v5).
  Không có state store thứ hai và không có PDF/OCR/full text trong các bảng này.
- Session và finding không thay đổi `logical_documents` canonical.
- Chỉ `ACCEPT` hoặc `MANUAL_FIX` được đưa vào repair plan. `KEEP_EXISTING` là
  quyết định bền vững nhưng không sửa kết quả hiện tại.
- Repair từ chối `STALE_REVIEW_BASE` nếu revision nền đã đổi; lệnh lặp lại một
  plan đã apply trả `ALREADY_APPLIED`, không tạo revision hay correction trùng.
- Repair đọc source bằng page objects để tạo artifact, kiểm SHA-256 nguồn trước
  và sau, đồng thời dùng rename plan hai pha sẵn có cho các nhóm naming bị ảnh
  hưởng. Mọi boundary mới phải cover 100% trang nguồn, không overlap và chỉ dùng
  type trong `document_types.json`.

## Scope và audit

`review-case` cho phép cả hồ sơ, một source SHA-256, hoặc `--pages 18-24`.
Audit local phát hiện coverage/overlap, artifact thiếu/orphan, filename không
khớp taxonomy, duplicate relation hỏng và low confidence. Semantic reviewer là
adapter explicit, tách khỏi runtime pipeline offline: nó render PNG tạm thời
bằng Poppler cho đúng source/page scope, gửi chỉ context scope đó tới endpoint
OpenAI-compatible và chỉ gọi `record_semantic_findings`. Không lưu ảnh/base64
hay payload model; finding phải qua enum/page/document/taxonomy/confidence/
evidence validation, nếu sai thì fail closed.

## CLI

```powershell
python -m app.cli review-case "input/<nguoi>"
python -m app.cli review-case "input/<nguoi>" --source-hash <sha> --pages 18-24
python -m app.cli review-semantic "input/<nguoi>" --source-hash <sha> --pages 18-24 --endpoint https://<approved-provider>/v1/chat/completions --model <vision-model>
python -m app.cli review-findings <session_id>
python -m app.cli review-approve <finding_id> --by operator
python -m app.cli review-reject <finding_id> --by operator
python -m app.cli review-manual-fix <finding_id> --payload '{"type_id":"07"}'
python -m app.cli repair-plan <session_id>
python -m app.cli repair <repair_plan_id> "input/<nguoi>"        # dry-run
python -m app.cli repair <repair_plan_id> "input/<nguoi>" --apply
python -m app.cli review-history "input/<nguoi>" --diff 1:2
python -m app.cli review-export-corrections "input/<nguoi>" --out review_corrections.jsonl
python -m app.cli review-benchmark-fixture <finding_id> --out benchmark/review_cases/example.json
```

Manual boundary operations must be explicit structured data. A merge supplies
`document_ids` and `source_pages`; a split supplies `documents`, each with
`source_pages` and intended classification. The validator rejects incomplete
coverage rather than guessing.

`review-semantic` needs the named environment key (`--api-key-env`, default
`OPENAI_API_KEY`); the key is never written to config, SQLite, or logs. Without
credential/model access it stops with `SEMANTIC_AI_RUNTIME_PENDING` and makes
no canonical mutation. The versioned prompt asks the model to review existing
results and return findings only. Repair paths are executable for reclassify,
merge, split, add missing document, remove extra overlapping document, duplicate
relation, filename, and legacy page-order metadata. A prior `KEEP_EXISTING`
suppresses only the same semantic fingerprint at the same base revision and
reviewer version.

Targeting is by source/page dependency: unchanged rows are not rewritten. A
filename repair may include its minimal global-naming dependency closure when
the requested destination is occupied; this is required for a safe two-phase
rename permutation, but it does not re-render/reclassify unrelated source pages.

Targeting is by source/page dependency: unchanged rows are not rewritten. A
filename repair may include its minimal global-naming dependency closure when
the requested destination is occupied; this is required for a safe two-phase
rename permutation, but it does not re-render/reclassify unrelated source pages.

## Windows Manager

The `Rà soát hồ sơ` section reads the same pipeline state when it exists beside
the configured input root. It starts deterministic audits, shows findings and
offers Keep/Accept/Manual actions. The repair panel defaults to dry-run and
shows the immutable-source/revision safety banner before an explicit Apply.

If local Manager config contains `semantic_review.endpoint` and
`semantic_review.model`, it exposes an explicit semantic-review button. The
credential remains only in the named environment variable.

## Recovery

Normal filesystem errors are rolled back by the existing two-phase rename
implementation and the SQLite transaction rolls canonical state back. A plan
that is not `READY`/`APPLIED` is intentionally blocked rather than retried
blindly; inspect `review-history`, `reconcile` and the plan record before a
new review session is created. This is fail-closed for interrupted repairs.

## Regression fixtures

Confirmed findings can be exported as anonymized metadata fixtures. No PDF,
person name, title, filename, source path or source hash is put in a generated
fixture. If an actual document is needed for a regression test, it must remain
outside Git or be replaced by a synthetic reproduction.
