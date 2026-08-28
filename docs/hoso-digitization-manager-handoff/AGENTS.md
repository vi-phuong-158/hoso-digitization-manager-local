# AGENTS.md — Hồ sơ Digitization Manager

## Mission

Build the offline local management UI described in the handoff package without destabilizing the existing digitization pipeline.

## Non-negotiable

1. Đọc toàn bộ handoff trước khi code.
2. Inspect repo trước khi chọn implementation.
3. Existing tests/behavior là evidence; không rewrite mù.
4. PDF/source folders read-only trong MVP.
5. Không upload dữ liệu nhạy cảm.
6. Không thêm cloud/API dependency cho core.
7. Bind localhost only.
8. Reuse official taxonomy.
9. Manifest/ledger integration read-only mặc định.
10. Ưu tiên implementation đơn giản.
11. Không mark PASS nếu chưa chạy validation.
12. Fix failures do mình tạo.
13. Không che legacy test fail; classify.
14. Commit theo phase.
15. Maintain `WORKING_LOG.md`.
16. Không hỏi confirmation giữa phase trừ blocker thực sự.
17. Khi conflict, data safety ưu tiên cao nhất.
18. Final phải có SHA, tests, packaging, limitations, verdict.

## Priority

1. Data safety
2. Correctness
3. Reuse pipeline
4. Offline reliability
5. Simplicity
6. Visual polish

## Forbidden

- Auto rename/delete/move user PDFs.
- Send filenames/content to third parties.
- External CDN runtime.
- Docker required for normal end-user operation.
- Rebuild document-classification engine không cần thiết.
