# Architecture

## 1. Tổng thể

```text
Windows Desktop
     |
     v
Local FastAPI Server
     |
     +---- Jinja2/HTMX UI
     +---- SQLite metadata DB
     +---- Filesystem Scanner
     |       +---- PDF folders
     |       +---- optional manifest/ledger
     +---- Audit/Logs
```

## 2. Vì sao server-rendered

Không dùng React/Vite ở MVP nếu repo chưa có frontend sẵn. Jinja2 + HTMX giảm dependency, dễ package PyInstaller, đủ cho dashboard/filter/CRUD và dễ để Luna hoàn thành end-to-end.

Nếu repo đã có frontend chuẩn và reuse rõ ràng hơn, agent có thể dùng lại nhưng phải ghi ADR.

## 3. Modules đề xuất

```text
app/
  main.py
  config.py
  db.py
  models/
  services/
    scanner.py
    parser.py
    progress.py
    status_engine.py
    taxonomy.py
    integration.py
  repositories/
  routes/
    dashboard.py
    cases.py
    scan.py
    settings.py
  templates/
  static/
  cli.py
tests/
  unit/
  integration/
  e2e/
data/
logs/
```

## 4. SQLite

Bật WAL. Scan dùng transaction nhỏ theo batch. Không lưu binary PDF.

## 5. Safety

Scanner read-only. MVP không rename/move/delete/overwrite file hồ sơ.

## 6. Incremental

Lưu `size`, `mtime_ns`, optional `sha256`. Nếu size + mtime không đổi thì không hash lại. Hash SHA-256 cho file mới/thay đổi.

## 7. Pipeline adapter

Interface `PipelineMetadataProvider`:
- `NoopProvider`
- `ManifestProvider`
- `LedgerProvider`

App luôn chạy được với Noop. Adapter manifest/ledger ưu tiên read-only.

## 8. Startup

1. load config;
2. validate data_root;
3. migrate/bootstrap DB;
4. start localhost;
5. open browser;
6. health check.

Bind mặc định `127.0.0.1`, không `0.0.0.0`.

## 9. Packaging

Target Windows 10/11 x64.

```text
HosoManager/
  HosoManager.exe
  config.json
  data/
  logs/
```

Không yêu cầu administrator.

## 10. Logging

Rotating `app.log`, `scan.log`. Không log nội dung PDF; chỉ metadata cần thiết.

## 11. Security

- localhost only
- no telemetry
- no external CDN
- static assets bundled
- path traversal protection
- canonical path validation
- CSRF/protection phù hợp cho POST
- HTML escaping
