# Product Specification

## 1. Tên làm việc

**Hồ sơ Digitization Manager** — codename source: `hoso-manager`.

## 2. Bài toán

Pipeline hiện hữu đã xử lý incremental, manifest, ledger, duplicate và review. Phần còn thiếu là lớp điều hành trực quan để biết ngay hồ sơ nào chưa làm, đang làm, chờ kiểm tra, cần bổ sung, sai quy tắc, có duplicate/review pending và tiến độ theo đơn vị.

## 3. Người dùng MVP

Một cán bộ vận hành trực tiếp trên máy Windows.

MVP không yêu cầu đăng nhập nhiều người, cloud, Internet, OCR, AI chat hoặc workflow phê duyệt nhiều cấp.

## 4. Nguồn dữ liệu

### 4.1 Folder hồ sơ

`[M1].[M2].[M3].[M4].[M5]_[CCCD]_[HoTenDangVien]`

Ví dụ:

`25.000.036.001.015_012345678901_NguyenVanA`

### 4.2 File tài liệu

`[STT].[Ten_tai_lieu].[SoThuTu].pdf`

`[SoThuTu]` tùy chọn.

Ví dụ:
- `01.Ly_lich_nguoi_xin_vao_dang.pdf`
- `55.Giay_gioi_thieu_sinh_hoat_dang_tam_thoi.1.pdf`
- `55.Giay_gioi_thieu_sinh_hoat_dang_tam_thoi.2.pdf`

### 4.3 Pipeline hiện hữu

Nếu có manifest/ledger do pipeline sinh ra, app được phép đọc để enrich metadata. App vẫn phải chạy khi chúng vắng mặt.

## 5. Trạng thái hồ sơ

- `CHUA_XU_LY`
- `DANG_SO_HOA`
- `CHO_KIEM_TRA`
- `CAN_BO_SUNG`
- `HOAN_THANH`

## 6. Trạng thái checklist tài liệu

- `CO_TAI_LIEU`
- `KHONG_PHAT_SINH`
- `CHUA_XAC_DINH`
- `CAN_BO_SUNG`

Không coi mọi tài liệu taxonomy là bắt buộc đối với mọi hồ sơ.

## 7. Cờ cảnh báo

- `THIEU_UU_TIEN_1`
- `SAI_TEN_FILE`
- `SAI_TEN_THU_MUC`
- `TRUNG_TAI_LIEU`
- `CAN_XAC_MINH`
- `REVIEW_PENDING`
- `FILE_KHONG_DOC_DUOC`
- `FILE_NGOAI_TAXONOMY`
- `CHANGED_AFTER_COMPLETION`

## 8. Sáu màn hình MVP

1. Dashboard
2. Danh sách hồ sơ
3. Chi tiết hồ sơ
4. Cần bổ sung
5. Chờ kiểm tra / Cảnh báo
6. Cấu hình

## 9. Dashboard

Hiển thị:
- tổng hồ sơ;
- chưa xử lý;
- đang số hóa;
- chờ kiểm tra;
- cần bổ sung;
- hoàn thành;
- thiếu ưu tiên 1;
- review pending;
- tiến độ tổng;
- tiến độ theo đơn vị/chi bộ;
- danh sách hồ sơ cần hành động.

## 10. Danh sách hồ sơ

Cột:
- Họ tên
- CCCD
- Đơn vị/chi bộ
- Số tài liệu
- Ưu tiên 1
- Tiến độ
- Trạng thái
- Cảnh báo
- Cập nhật cuối

Có search họ tên/CCCD; filter status/unit/warning/missing P1; sort theo cập nhật, tiến độ, họ tên.

## 11. Chi tiết hồ sơ

Header: họ tên, CCCD, M1-M5, trạng thái, tiến độ, folder.

Sections:
- Checklist taxonomy
- Files thực tế
- Cảnh báo
- Lịch sử trạng thái
- Ghi chú

Actions:
- mở PDF;
- mở folder;
- đánh dấu `KHONG_PHAT_SINH`;
- đánh dấu `CAN_BO_SUNG`;
- bỏ override;
- đánh dấu hoàn thành;
- mở lại hồ sơ.

## 12. Scanner

Nút `Quét lại dữ liệu` phải:
1. duyệt folder hồ sơ;
2. parse folder;
3. parse file;
4. tính checksum chỉ khi file mới/thay đổi;
5. cập nhật DB incremental;
6. không tạo record trùng;
7. đánh dấu missing thay vì xóa cứng;
8. sinh warnings;
9. cập nhật summary;
10. lưu scan run.

## 13. Progress

Weighted checklist fallback:
- P1 = 3
- P2 = 2
- P3+ = 1

Completed khi `CO_TAI_LIEU` hoặc `KHONG_PHAT_SINH`.

`CHUA_XAC_DINH` và `CAN_BO_SUNG` chưa hoàn thành.

Nếu taxonomy hiện hữu có priority/applicability chính thức thì reuse.

## 14. Auto status

Không có file hợp lệ -> `CHUA_XU_LY`.

Có review pending -> `DANG_SO_HOA`.

Thiếu P1 hoặc checklist `CAN_BO_SUNG` -> `CAN_BO_SUNG`.

Đã resolve nhưng chưa final review -> `CHO_KIEM_TRA`.

`HOAN_THANH` chỉ qua thao tác explicit của người dùng.

## 15. Non-goals MVP

Không sửa PDF, OCR, cloud, chữ ký số, sync nhiều máy, phân quyền, web public, mobile native hoặc AI tự quyết định hoàn thành.
