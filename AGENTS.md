# HosoManager Local — Development rules

This repository is the standalone Windows/local manager for already-digitized party records.

- It operates only on a configurable local `data_root` (default `done/output`).
- The SQLite database is an index/metadata store; PDFs remain on the filesystem.
- Never commit runtime data, PDFs, images, SQLite files, logs, config files, or absolute operator paths.
- `document_types.json` is the versioned shared 104-type contract with the Pipeline repository. Do not create a 105th type.
- Keep Manager independent: no imports from, installation of, or runtime dependency on the Pipeline repository.
- Scan/AI is an optional filesystem integration page only; classification/segmentation/review-repair engines belong to the Pipeline repository.
- Document viewer routes must resolve a document id through Manager metadata, enforce `data_root` containment, and use inline PDF responses.
- Validate source changes with Manager tests, compileall, and Windows build before release.