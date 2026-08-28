# Hồ sơ Digitization Manager — Handoff Package

## Mục tiêu

Xây dựng một ứng dụng nội bộ, offline-first, chạy trên Windows để:

- Theo dõi tiến độ số hóa hồ sơ.
- Quản lý từng hồ sơ/case.
- Tự quét cấu trúc thư mục và tên file PDF hiện có.
- Tự nhận biết tài liệu đã có, thiếu, sai tên, trùng hoặc cần kiểm tra.
- Tận dụng pipeline số hóa hiện hữu thay vì viết lại engine phân loại tài liệu.
- Không đưa PDF vào database.
- Không phụ thuộc cloud/API bên ngoài cho chức năng lõi.

## Nguyên tắc sản phẩm

1. File PDF và thư mục hiện có là nguồn dữ liệu gốc.
2. SQLite chỉ lưu metadata, trạng thái, ghi chú, lịch sử thao tác.
3. App không tự ý đổi/xóa PDF ở MVP.
4. Tất cả thao tác có thể tái dựng từ filesystem + manifest.
5. MVP ưu tiên độ ổn định, rõ ràng và dùng được ngay hơn tính năng phức tạp.
6. Hoạt động hoàn toàn offline.
7. Tích hợp với pipeline số hóa hiện có qua filesystem/manifest/ledger, không fork lại logic lõi.

## Stack đề xuất

- Backend: Python 3.12 + FastAPI
- DB: SQLite
- ORM: SQLModel hoặc SQLAlchemy 2.x
- Frontend: Jinja2 + HTMX + Alpine.js (ưu tiên đơn giản)
- CSS: local-only
- PDF preview: browser native PDF viewer
- Packaging Windows: PyInstaller
- Testing: pytest + Playwright

## Cấu trúc gói

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
- `MASTER_PROMPT_LUNA.md`
- `example-config.json`

## Verdict mong muốn cuối cùng

`DIGITIZATION_MANAGER_MVP_READY_FOR_LOCAL_PILOT`

Chỉ coi là hoàn thành khi toàn bộ acceptance gate đạt.
