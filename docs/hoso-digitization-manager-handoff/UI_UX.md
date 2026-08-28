# UI / UX

## Direction

- Desktop-first, tiếng Việt.
- Công cụ nghiệp vụ, không dashboard SaaS màu mè.
- Light mode.
- Responsive đủ cho tablet.
- Không CDN.
- Be Vietnam Pro chỉ dùng nếu bundle local hợp lệ; nếu không dùng system font.

## Sidebar

- Tổng quan
- Hồ sơ
- Cần bổ sung
- Chờ kiểm tra
- Cảnh báo
- Cấu hình

Hiện lần quét gần nhất và nút `Quét lại`.

## Dashboard

Cards: Tổng, Hoàn thành, Đang xử lý, Chờ kiểm tra, Cần bổ sung, Thiếu ƯT1.

Thêm progress tổng, tiến độ theo đơn vị, hồ sơ cần hành động, scan gần nhất.

## Case list

Sticky filters: search, status, unit, warning, missing P1. Table row click mở detail.

Màu không được là tín hiệu duy nhất; luôn có text/icon.

## Case detail

Header: tên, CCCD, M1-M5, status, progress.

Actions: mở folder, quét lại case, mark complete/reopen, ghi chú.

Checklist group theo priority. Mỗi row: code, tên taxonomy, status, số file, action.

Expandable file list: filename, size, modified, open.

## Scan UX

Không freeze browser. Nếu scan dài, hiển thị progress/polling. Scan fail phải giữ dữ liệu cũ và chỉ rõ log.

## Empty/error states

Phải có cho: chưa cấu hình root, chưa có hồ sơ, filter không kết quả, scan lỗi.

## Accessibility

Keyboard nav, semantic controls, focus state, contrast, table headers, form errors.
