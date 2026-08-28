# Acceptance Tests

## A. Filesystem safety
- [ ] Scan không rename/move/delete/modify PDF.
- [ ] Malformed data không crash toàn scan.

## B. Folder parsing
- [ ] Folder chuẩn parse đủ M1-M5, CCCD, name.
- [ ] Folder sai tên vẫn xuất hiện và có warning.

## C. Document parsing
- [ ] Filename chuẩn map taxonomy.
- [ ] `.1`, `.2` nhận đúng sequence.
- [ ] Unknown/malformed vẫn inventory + warning.

## D. Incremental
- [ ] Scan 2 lần không duplicate rows.
- [ ] Unchanged file không rehash.
- [ ] Changed file update.
- [ ] Deleted -> is_present=false.
- [ ] Reappeared -> true.

## E. Duplicate
- [ ] Same SHA in one case -> warning.
- [ ] Không tự xóa.

## F. Checklist
- [ ] Existing mapped file -> CO_TAI_LIEU.
- [ ] KHONG_PHAT_SINH persist.
- [ ] CAN_BO_SUNG persist.
- [ ] File mới có thể làm effective thành CO_TAI_LIEU nhưng vẫn giữ history override.

## G. Status
- [ ] No docs -> CHUA_XU_LY.
- [ ] Missing P1 -> CAN_BO_SUNG.
- [ ] Review pending -> DANG_SO_HOA.
- [ ] Resolved not final-reviewed -> CHO_KIEM_TRA.
- [ ] Explicit completion only -> HOAN_THANH.
- [ ] Reopen works.
- [ ] Change after completion -> review warning.

## H. Progress
- [ ] Weighted deterministic.
- [ ] KHONG_PHAT_SINH counts completed.
- [ ] CAN_BO_SUNG/CHUA_XAC_DINH not completed.

## I. UI
- [ ] Dashboard counts correct.
- [ ] Search name/CCCD.
- [ ] Filters work.
- [ ] Case detail works.
- [ ] PDF/folder open limited to data root.
- [ ] Vietnamese labels readable.

## J. Security
- [ ] localhost only.
- [ ] no external analytics/CDN.
- [ ] path traversal rejected.
- [ ] state-changing POST protected.

## K. Packaging
- [ ] Windows build launches without dev command.
- [ ] DB/config persist.
- [ ] Browser opens.
- [ ] Works without Internet.

## L. Regression
- [ ] Existing pipeline tests pass.
- [ ] Existing PDFs unchanged.
- [ ] Existing manifest/ledger not mutated unless explicitly proven required and safe.

## M. Pilot report
Must include folders, PDFs, malformed, duplicates, missing P1, first/second scan time, test counts, packaging result, exact SHA.
