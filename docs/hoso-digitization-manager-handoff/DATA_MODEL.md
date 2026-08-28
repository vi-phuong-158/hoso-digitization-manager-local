# Data Model

## cases

- `id` INTEGER PK
- `case_key` TEXT UNIQUE NOT NULL
- `folder_path` TEXT UNIQUE NOT NULL
- `folder_name` TEXT NOT NULL
- `m1`..`m5` TEXT NULL
- `citizen_id` TEXT NULL
- `person_name_raw` TEXT NULL
- `person_name_display` TEXT NULL
- `unit_code` TEXT NULL
- `auto_status` TEXT NOT NULL
- `manual_status` TEXT NULL
- `effective_status` TEXT NOT NULL
- `progress_percent` REAL NOT NULL DEFAULT 0
- `document_count` INTEGER NOT NULL DEFAULT 0
- `warning_count` INTEGER NOT NULL DEFAULT 0
- `missing_priority1_count` INTEGER NOT NULL DEFAULT 0
- `is_present` BOOLEAN DEFAULT 1
- `first_seen_at`, `last_seen_at`, `last_scanned_at`
- `completed_at` NULL
- `reviewed_by` TEXT NULL
- `note` TEXT NULL

## documents

- `id` INTEGER PK
- `case_id` FK
- `relative_path` TEXT UNIQUE NOT NULL
- `filename` TEXT NOT NULL
- `taxonomy_code` TEXT NULL
- `taxonomy_name` TEXT NULL
- `sequence_no` INTEGER NULL
- `priority` INTEGER NULL
- `size_bytes`, `mtime_ns`
- `sha256` TEXT NULL
- `parse_status` TEXT
- `is_present` BOOLEAN DEFAULT 1
- timestamps

Indexes: `case_id`, `taxonomy_code`, `sha256`, `is_present`.

## taxonomy_items

- `code` TEXT PK
- `name` TEXT NOT NULL
- `priority` INTEGER DEFAULT 3
- `active` BOOLEAN DEFAULT 1
- `default_applicability` TEXT DEFAULT `CHUA_XAC_DINH`

Seed từ taxonomy chính thức hiện hữu nếu có; không tạo taxonomy thứ hai khi repo đã có source of truth.

## checklist_overrides

- `id`
- `case_id`
- `taxonomy_code`
- `status`
- `note`
- `updated_at`
- UNIQUE(case_id, taxonomy_code)

## warnings

- `id`
- `case_id` NULL
- `document_id` NULL
- `warning_type`
- `severity`: INFO/WARNING/ERROR
- `message`
- `active`
- `fingerprint` UNIQUE
- timestamps

## case_history

Events:
- SCAN_DISCOVERED
- AUTO_STATUS_CHANGED
- MANUAL_STATUS_CHANGED
- CHECKLIST_OVERRIDE
- NOTE_UPDATED
- MARK_COMPLETED
- REOPENED

## scan_runs

- start/end/status
- folders_seen/files_seen
- cases_created/updated
- docs_created/updated
- warnings_created
- errors
- duration_ms
