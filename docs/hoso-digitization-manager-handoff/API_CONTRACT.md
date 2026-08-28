# Internal API Contract

- `GET /health`
- `GET /` dashboard
- `GET /cases?q=&status=&unit=&warning=&missing_p1=&page=&sort=`
- `GET /cases/{case_id}`
- `POST /cases/{case_id}/status`
- `POST /cases/{case_id}/complete`
- `POST /cases/{case_id}/reopen`
- `POST /cases/{case_id}/checklist/{taxonomy_code}`
- `POST /cases/{case_id}/note`
- `POST /scan`
- `POST /scan/{case_id}`
- `GET /scan-runs`
- `GET /settings`
- `POST /settings`
- `GET /open/case/{case_id}`
- `GET /open/document/{document_id}`
- `GET /backup` optional

Không endpoint nào nhận arbitrary path để mở file. Chỉ dùng case/document id đã resolve trong DB và validate dưới root.

Errors: 400 validation, 404 missing, 409 conflict, 500 internal. UI lỗi bằng tiếng Việt; stack trace chỉ log local.
