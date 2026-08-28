# Pipeline số hóa hồ sơ Đảng viên

Đọc `AGENTS.md` trước. File này chỉ mô tả **code**; hợp đồng vận hành nằm ở `AGENTS.md`,
quy trình chạy hằng ngày nằm ở `RUNBOOK_ANTIGRAVITY.md`, giới hạn còn lại nằm ở `LIMITATIONS.md`.

## Luồng

```
input/<người>/*.pdf
   -> Phase A  pdf_inventory   liệt kê + SHA-256 + khổ trang (chỉ đọc)
   -> Phase B  vision_adapter  tín hiệu CẤP TRANG do Agent trả về (analysis/*.json)
   -> Phase C  segmenter       gom trang thành logical document (bìa/mặt sau/trang tiếp)
   -> Phase D  classifier      phân loại cả tài liệu + chính sách confidence
   -> Phase E  naming          tên file deterministic từ document_types.json
   -> Phase F  writer          dry-run (mặc định) / apply, tách bằng page object
   -> Phase G  qc              coverage, overlap, hash nguồn, tên file, collision
   -> output/<người>/ , review/<người>/ , logs/<người>/
```

## Module

| File | Vai trò |
|------|---------|
| `app/catalog.py` | Đọc `document_types.json`, hàng rào `type_id`, nhóm dễ nhầm theo AGENTS.md mục 6 |
| `app/pdf_inventory.py` | Inventory chỉ đọc, SHA-256, khổ trang, so khổ trang |
| `app/vision_adapter.py` | Interface `DocumentVisionProvider` + registry + validate output của model |
| `app/providers/agent_provider.py` | **Provider runtime**: đọc JSON do Antigravity Agent ghi ra |
| `app/agent_contract.py` | Hợp đồng JSON Agent -> code + validator cứng |
| `app/providers/fixture_provider.py` | Provider fixture cho test/golden |
| `app/providers/gemini_provider.py` | `NOT_USED_IN_ANTIGRAVITY_RUNTIME` — adapter API cũ, không đăng ký, không nằm trong runtime path |
| `app/segmenter.py` | Ranh giới tài liệu ở cấp trang |
| `app/classifier.py` | Phân loại + ngưỡng AUTO / second pass / REVIEW |
| `app/naming.py` | Naming engine deterministic |
| `app/manifest.py` | Manifest truy ngược |
| `app/writer.py` | Tách/ghi file, idempotent, fail-safe |
| `app/qc.py` | Hàng rào bất biến |
| `app/pipeline.py` | Điều phối A→G |
| `app/golden.py` | Golden acceptance |
| `app/state.py` | State registry (SQLite local): 6 trạng thái nguồn + bảng `logical_documents` (đơn vị resolve/naming) |
| `app/fingerprint.py` | Fingerprint cache (taxonomy + schema hợp đồng) — quyết định cache còn dùng được hay STALE |
| `app/incremental.py` | Đối chiếu inventory + fingerprint với state registry -> NEW/STALE/CACHED/PROCESSED/... |
| `app/global_naming.py` | Đặt tên `.1/.2/...` nhìn TOÀN BỘ hồ sơ (không chỉ lượt chạy) + rename plan 2 pha fail-safe |
| `app/policy.py` | DEV POLICY CLOSURE: type 87 subtype, SUPPORTING_DOCUMENT, DUPLICATE, partial date precision (DAY/MONTH/YEAR) |
| `app/policy_rehearsal.py` | Rehearsal thuần dữ liệu (đọc manifest/analysis đã freeze) — preview AUTO/REVIEW trước/sau khi áp policy, không mutate state |
| `app/review.py` | `review-list`/`resolve-review` — người vận hành chốt REVIEW (TAXONOMY/SUPPORTING/DUPLICATE/ngày) không cần Agent đọc lại PDF |
| `app/review_repair.py` | Audit kết quả đã xử lý, session/finding/decision, repair plan targeted, revision và correction ledger |
| `app/semantic_reviewer.py` | Adapter semantic review explicit ngoài offline runtime path; render scope tạm thời và chỉ tạo proposal đã validate |
| `app/semantic_reviewer.py` | Adapter semantic review explicit ngoài offline runtime path; render scope tạm thời và chỉ tạo proposal đã validate |
| `app/semantic_reviewer.py` | Adapter semantic review explicit, ngoài offline runtime path; render scope tạm thời và chỉ tạo proposal đã validate |
| `app/reconcile.py` | Đối chiếu state DB với file thật trên đĩa (`reconcile`), chỉ báo cáo |
| `app/state_import.py` | Migration: nạp PROCESSED từ manifest/output đã có sẵn (`import-state`) |
| `app/cli.py` | CLI |

## Hai trục trạng thái (quan trọng)

Một logical document có **hai** trạng thái, đừng nhầm:

- `classification_status` — trục **phân loại** (Phase D). Golden `expected_review` đối chiếu trục này.
- `final_status` — trạng thái **cuối** sau Phase E. Một tài liệu phân loại chắc chắn (AUTO)
  vẫn có thể thành REVIEW nếu nhóm cùng loại không xếp được thứ tự thời gian —
  đúng quy tắc AGENTS.md Phase E ("không tự đánh `.1/.2` theo thứ tự scan").

Chỉ tài liệu có `final_status = AUTO` mới được mang tên chuẩn và vào `output/`.

## Runtime Antigravity-native

Runtime **không gọi API AI qua mạng và không cần API key**. Kiến trúc:

```
PDF trong workspace
   -> Antigravity Runtime Agent  (nhận thức: đọc trang, gom tài liệu, phân loại)
   -> analysis/<người>/<pdf>.json   (hợp đồng app/agent_contract.py)
   -> validator local  (từ chối JSON sai: type lạ, thiếu/lặp/chồng trang, tên file, sai kiểu)
   -> segmenter local  (tự gom lại, đối chiếu chéo với đề xuất của Agent)
   -> classifier -> naming -> split/copy -> AUTO/REVIEW -> manifest + QC
```

| Đường dẫn | Vai trò |
|-----------|---------|
| `.agents/rules/party-record-digitization.md` | 18 luật khóa cứng cho mọi agent trong workspace |
| `.agents/agents/document-processor/agent.md` | Custom Runtime Agent + schema JSON |
| `.agents/workflows/process-party-record.md` | Workflow `/process-party-record`, dừng ở dry-run |
| `analysis/<người>/<pdf>.json` | Output nhận thức của Agent (đầu vào của pipeline) |

Hai hàng rào kiến trúc được test giữ (`tests/test_runtime_no_network.py`):
runtime path không import thư viện mạng, không đọc biến môi trường dạng
API key/token/secret, và pipeline chạy trọn vẹn khi `socket` bị chặn.

## Incremental processing + global naming

`process_person_folder(..., state_registry=StateRegistry(...))` là đường đi mặc
định của CLI. `process_person_folder(..., state_registry=None)` (mặc định của
hàm khi gọi trực tiếp) giữ nguyên hành vi gốc — xử lý toàn bộ nguồn mỗi lần,
dùng bởi `golden.py`/test cũ để không phụ thuộc state.

Hai khái niệm tách biệt trong `app/state.py`:

- **AI đã đọc xong** — `ANALYZED_PENDING_APPLY` (không cần review) hoặc
  `REVIEW_REQUIRED` (còn logical document cần người chốt). Gắn fingerprint
  (taxonomy + schema); fingerprint đổi -> `STALE_ANALYSIS`, đọc lại.
- **Nghiệp vụ đã xong** — `PROCESSED`, chỉ khi apply thành công + QC PASS +
  không còn logical document nào `REVIEW_PENDING`.

Đặt tên `.1/.2/...` (`app/global_naming.py`) nhìn **toàn bộ** logical document
đã biết của một người qua bảng `logical_documents`, không chỉ nguồn xử lý trong
lượt hiện tại — chèn tài liệu cũ hơn có thể đổi tên file đã ghi từ lượt trước,
thực thi bằng kế hoạch rename 2 pha fail-safe (`execute_rename_plan`).

Chi tiết trạng thái/transition/lệnh: `RUNBOOK_ANTIGRAVITY.md`.

## Ranh giới model

Business logic không import provider nào. Model chỉ được phép trả về:

- tín hiệu **cấp trang** (`analyze_pages`);
- kết luận loại cho **một logical document** (`classify_document`).

Model **không** được: gom nhóm trang, đặt tên file, đánh số thứ tự, tạo `type_id` mới.
Mọi output của model đi qua `validate_page_observation` / `validate_classification`.

Thêm provider mới = thêm một file trong `app/providers/` và gọi `register_provider(...)`.

## Chạy

```bash
python -m pytest tests -q
```

Review & Repair (không tự apply): xem [docs/review-repair-pipeline.md](docs/review-repair-pipeline.md).

```bash
python -m app.cli test-golden --provider agent
```

`test-golden` luôn dựng một workspace tạm với PDF trắng synthetic (đúng tên và
số trang trong contract), page-signal fixture và agent-analysis fixture đã
được kiểm soát. Lệnh không đọc/ghi `input/`, `analysis/`, `output/`, `review/`
hay `state/` production; fixture sẽ bị dọn sau khi chạy. Vì vậy Golden không
phụ thuộc vào byte PDF hồ sơ cá nhân.

```bash
python -m app.cli process "input/<CASE_NAME>"
```

Provider mặc định của `process` là `agent` (đọc `analysis/`). `fixture` chỉ dùng cho test.

## Batch một lệnh

Để xử lý toàn bộ `input/<người>/` theo chính sách AUTO_SAFE:

```bash
python -m app.cli batch-run input
```

Batch chỉ apply hồ sơ không có `REVIEW_PENDING`, `MISSING_SOURCE`, lỗi QC,
collision hay lệch reconciliation. Hồ sơ còn mơ hồ được giữ nguyên để operator
quyết định, còn các hồ sơ độc lập khác vẫn tiếp tục. Dùng `--dry-run` (hoặc
`--no-apply`) để chỉ validate/freeze analysis, và `--person "Tên người"` để
giới hạn một hồ sơ. Report cuối được ghi tại `logs/batch-report.json`.
## Public snapshot

This public repository excludes production databases, PDFs, images, runtime outputs, logs, and Golden fixtures derived from private records. The 	est-golden command therefore requires separately supplied synthetic fixtures and is not part of the public regression suite.