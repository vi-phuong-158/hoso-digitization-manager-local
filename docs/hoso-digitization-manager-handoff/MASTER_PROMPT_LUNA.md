# MASTER PROMPT — Build Hồ sơ Digitization Manager End-to-End

Bạn đang làm việc trực tiếp trong repository của hệ thống số hóa hồ sơ hiện có.

## MỤC TIÊU

Build hoàn chỉnh một ứng dụng local/offline để theo dõi tiến độ và quản lý hồ sơ số hóa, tận dụng pipeline hiện hữu.

Final desired verdict:

`DIGITIZATION_MANAGER_MVP_READY_FOR_LOCAL_PILOT`

Không dừng ở prototype/mockup hoặc kế hoạch. Thực thi end-to-end: khảo sát repo → kiến trúc phù hợp baseline → code → test → tích hợp → đóng gói Windows → báo cáo nghiệm thu.

## 0. Đọc trước khi sửa code

Đọc toàn bộ:
- `README.md` handoff
- `PRODUCT_SPEC.md`
- `ARCHITECTURE.md`
- `DATA_MODEL.md`
- `STATE_MACHINE.md`
- `FILESYSTEM_RULES.md`
- `UI_UX.md`
- `API_CONTRACT.md`
- `TASKS.md`
- `ACCEPTANCE_TESTS.md`
- `AGENTS.md`

Handoff mô tả intent; repo hiện tại là source of truth cho pipeline/taxonomy/schema đã có.

Nếu conflict, ưu tiên:
1. an toàn dữ liệu;
2. behavior có test;
3. intent spec;
và ghi decision.

## 1. Luật làm việc

- Không hỏi người dùng từng bước.
- Tự chạy lần lượt phase trong `TASKS.md`.
- Chỉ dừng nếu blocker thật sự không thể giải quyết local.
- Không trả lại hàng loạt task vụn.
- Sau mỗi phase: test → fix → commit → append `WORKING_LOG.md` → tiếp tục.
- Không tuyên bố PASS bằng đọc code.
- Không bỏ qua CI/test fail do thay đổi của mình.
- Không merge main trái convention repo.
- Không phá dữ liệu thật.
- Không rename/move/delete/overwrite PDF.

## 2. Baseline bắt buộc

Trước code:
1. `git status`
2. branch, HEAD, remote
3. tree repo
4. tìm taxonomy, manifest, ledger SQLite, duplicate/review logic, CLI, pipeline, tests, packaging
5. chạy baseline tests
6. ghi `BASELINE.md`

Nếu working tree dirty không liên quan: không xóa/overwrite; dùng branch/worktree nếu cần.

## 3. Kiến trúc mặc định

Ưu tiên Python 3.12 + FastAPI + SQLite + Jinja2 + HTMX + local CSS + PyInstaller.

Nếu repo có stack tương đương rõ ràng và reuse tốt hơn, được dùng nhưng ghi ADR.

Core app offline, không phụ thuộc CDN, Supabase, Firebase, API AI, analytics hoặc Internet. Bind `127.0.0.1`.

## 4. Chức năng bắt buộc

### Dashboard
Tổng hồ sơ, chưa xử lý, đang số hóa, chờ kiểm tra, cần bổ sung, hoàn thành, thiếu P1, review pending, progress tổng, tiến độ theo đơn vị, hồ sơ cần hành động.

### Danh sách
Search họ tên/CCCD; filter status/unit/warning/missing P1; sort.

### Chi tiết
Metadata folder; checklist taxonomy; files; warnings; history; note; mở PDF/folder; checklist override; mark complete; reopen.

### Scanner
Read-only; incremental; idempotent; malformed safe; checksum only when changed; duplicate warning; missing reconciliation; scan stats.

### Checklist statuses
`CO_TAI_LIEU`, `KHONG_PHAT_SINH`, `CHUA_XAC_DINH`, `CAN_BO_SUNG`.

Không giả định mọi taxonomy item bắt buộc cho mọi hồ sơ.

### Case statuses
`CHUA_XU_LY`, `DANG_SO_HOA`, `CHO_KIEM_TRA`, `CAN_BO_SUNG`, `HOAN_THANH`.

`HOAN_THANH` chỉ explicit user action.

### Warnings
`THIEU_UU_TIEN_1`, `SAI_TEN_FILE`, `SAI_TEN_THU_MUC`, `TRUNG_TAI_LIEU`, `CAN_XAC_MINH`, `REVIEW_PENDING`, `FILE_KHONG_DOC_DUOC`, `FILE_NGOAI_TAXONOMY`, `CHANGED_AFTER_COMPLETION`.

## 5. Pipeline integration

Không viết lại document processing engine.

Tìm contract thực tế của taxonomy/manifest/ledger/duplicate/review_pending và tạo adapter read-only.

App vẫn chạy khi adapter không có dữ liệu.

Nếu schema không đủ bằng chứng: không đoán; dùng filesystem; ghi limitation.

## 6. Progress

Nếu taxonomy có priority chính thức thì dùng.

Fallback P1=3, P2=2, P3+=1.

Completed: CO_TAI_LIEU, KHONG_PHAT_SINH.
Not completed: CHUA_XAC_DINH, CAN_BO_SUNG.

Unit test formula + edge cases.

## 7. Security/Data safety

localhost only; no telemetry/upload/CDN/external fonts; canonical path validation; path traversal rejected; source PDF read-only; state-changing POST protection; không log nội dung PDF.

## 8. Synthetic fixtures

Ít nhất:
1. folder chuẩn
2. folder malformed
3. standard PDF names
4. multi-instance .1/.2
5. unknown file
6. duplicate bytes
7. missing P1
8. deleted/reappeared file
9. no-file case
10. completed then modified
11. checklist overrides

Không dùng dữ liệu nhạy cảm thật cho automated tests.

## 9. Test gates

Hoàn tất `ACCEPTANCE_TESTS.md`.

Có unit, integration scan, route tests, Playwright/smoke E2E và regression pipeline cũ.

Nếu repo có CI, tích hợp tests mới hợp lý và không phá CI cũ.

## 10. Windows packaging

Tạo reproducible build.

Expected:
- `HosoManager.exe` hoặc bundle tương đương
- double click
- localhost server
- browser opens
- config/data/log writable
- no Internet

Nếu môi trường hiện tại không thể chứng minh Windows executable, tạo spec/script và validate tối đa nhưng final verdict không được là READY; dùng `DIGITIZATION_MANAGER_TECHNICAL_PASS_RUNTIME_GATE_PENDING`.

## 11. Performance

Đo first scan và second unchanged scan. Second scan phải chứng minh incremental và không rehash unchanged files.

## 12. UI

Tiếng Việt, nghiệp vụ, không fake production data, có empty/error states. Be Vietnam Pro chỉ bundle local nếu có sẵn/được phép.

## 13. Work log

Tạo `WORKING_LOG.md`, append timestamp, phase, files changed, decisions, commands/tests, result, commit SHA.

## 14. Final validation

Trước report:
- git status
- full tests
- static/lint
- legacy regression
- app smoke
- fixture scan
- verify source PDF hashes unchanged
- packaging
- no external runtime requests required
- exact HEAD SHA

## 15. Final report format

### VERDICT
Một trong:
- `DIGITIZATION_MANAGER_MVP_READY_FOR_LOCAL_PILOT`
- `DIGITIZATION_MANAGER_TECHNICAL_PASS_RUNTIME_GATE_PENDING`
- `DIGITIZATION_MANAGER_NOT_READY`

### BASELINE
branch, starting SHA, final SHA

### IMPLEMENTED
Feature thực tế

### DATA SAFETY
source PDFs modified? YES/NO
rename/move/delete? YES/NO
external upload? YES/NO

### INTEGRATION
taxonomy source, manifest source, ledger source, fallback behavior

### VALIDATION
unit, integration, E2E, legacy regression, benchmark, packaging

### PILOT SUMMARY
folders, PDFs, malformed, duplicates, missing P1, first scan time, second scan time

### KNOWN LIMITATIONS
Chỉ limitation thực

### NEXT ACTION
Một hành động rõ ràng

Không kết thúc bằng kế hoạch. Hãy build và chứng minh kết quả.
