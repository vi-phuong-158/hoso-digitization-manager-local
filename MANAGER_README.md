# HosoManager local

HosoManager là ứng dụng Windows chạy local trên `127.0.0.1`. Kho tài liệu mặc định là `done\output`; mỗi thư mục con là một hồ sơ. SQLite chỉ lưu index và metadata, không lưu PDF.

## Chức năng chính

- `Hồ sơ Đảng viên`: quét, tìm kiếm, xem checklist 104 loại và mở thư mục.
- `Thêm tài liệu`: chọn nhiều JPG/JPEG/PNG/PDF, xem thumbnail, kéo thả, xoay, xóa trang, chọn type từ `document_types.json`, xem tên dự kiến và lưu atomic vào hồ sơ.
- `Scan / AI`: màn hình riêng cho quét lại kho; không phải fallback của các menu khác.
- `Rà soát hồ sơ`: giữ nguyên Review & Repair hiện có.
- `Sao lưu`: tạo bản sao SQLite metadata; để backup đầy đủ, sao chép thêm toàn bộ `done\output`.
- `Cài đặt`: đổi data root canonical và quét lại.

File được chọn trong luồng manual add chỉ được đọc. Ảnh/PDF staging tạm thời được dọn sau khi lưu hoặc hủy; nguồn không bị rename, move, delete hay overwrite.

## Chạy

```powershell
python -m app.manager.entrypoint
```

Ứng dụng không cần tài khoản, cloud, API AI hay server ngoài máy.
