# Filesystem Rules

## Data root

Config `data_root`. Mặc định mỗi direct child folder dưới root là một case.

## Folder parser

Regex định hướng:

```regex
^(?P<m1>[^.]+)\.(?P<m2>[^.]+)\.(?P<m3>[^.]+)\.(?P<m4>[^.]+)\.(?P<m5>[^_]+)_(?P<cccd>[^_]+)_(?P<name>.+)$
```

Không assume M1-M5 chỉ là số nếu dữ liệu thật có ngoại lệ.

Parse fail vẫn tạo case, warning `SAI_TEN_THU_MUC`, case_key dùng normalized relative path.

## Filename parser

Dạng `CODE.Name[.Sequence].pdf`.

- extension case-insensitive;
- code segment đầu;
- sequence là số segment cuối khi phù hợp;
- phần giữa là slug/name.

Code hợp lệ nhưng name khác canonical: vẫn map theo code, có thể warning mismatch.

Không parse được: vẫn inventory, taxonomy_code NULL, warning phù hợp.

## Duplicate

Cùng SHA-256 trong cùng case -> `TRUNG_TAI_LIEU`. Nếu ledger pipeline đã xác định duplicate chính thức, ưu tiên ledger. Không tự xóa.

## Missing

File/folder biến mất -> `is_present=false`, giữ history; không hard delete.

## Ignore

Bỏ qua hidden/system/temp: `.DS_Store`, `Thumbs.db`, `~$*`, configured patterns.

## Symlink

Không follow symlink/junction ra ngoài root.

## Path security

Mọi endpoint mở file/folder phải canonicalize và xác minh vẫn nằm dưới `data_root`.
