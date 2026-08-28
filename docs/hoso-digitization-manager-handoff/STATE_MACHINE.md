# State Machine

```text
CHUA_XU_LY
   |
   v
DANG_SO_HOA
   | \
   |  +----> CAN_BO_SUNG
   v             |
CHO_KIEM_TRA <---+
   |
   v
HOAN_THANH
```

## Effective status

`manual_status` ưu tiên nếu khác NULL.

Nếu hồ sơ đã `HOAN_THANH` nhưng filesystem thay đổi sau `completed_at`, không tự hạ trạng thái; sinh `CHANGED_AFTER_COMPLETION` và yêu cầu re-review.

## Auto status

```python
if valid_document_count == 0:
    return CHUA_XU_LY
if has_active_review_pending:
    return DANG_SO_HOA
if missing_priority1_count > 0:
    return CAN_BO_SUNG
if has_manual_checklist_can_bo_sung:
    return CAN_BO_SUNG
return CHO_KIEM_TRA
```

Không auto return `HOAN_THANH`.

## Checklist effective

1. Nếu có file hợp lệ -> `CO_TAI_LIEU`.
2. Nếu không có file và có override -> override.
3. Nếu không -> `CHUA_XAC_DINH`.

Nếu file xuất hiện sau `KHONG_PHAT_SINH`, effective display chuyển `CO_TAI_LIEU`, history vẫn giữ.

## Progress

P1=3, P2=2, P3+=1.

Completed: `CO_TAI_LIEU`, `KHONG_PHAT_SINH`.

Not completed: `CHUA_XAC_DINH`, `CAN_BO_SUNG`.
