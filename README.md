# HosoManager Local

HosoManager Local is an offline Windows application for managing already-digitized party-record PDFs on the operator's machine. It serves only on localhost; data is not uploaded. PDFs remain under a configurable `data_root` (default `done/output`) while SQLite stores metadata and index records only.

Features include dashboard, case list, 104-type checklist, manual document add from JPG/JPEG/PNG/PDF, page reorder/rotation/removal, deterministic naming, local rescan, metadata backup, settings, Windows folder open, and inline PDF viewing in the browser.

## Run locally

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.manager.main:app --host 127.0.0.1 --port 8765
```

Set a portable local configuration through `manager-config.json` (ignored by Git):

```json
{
  "data_root": "done/output",
  "database_path": "data/manager.db",
  "host": "127.0.0.1",
  "port": 8765,
  "open_browser_on_start": true
}
```

## Development checks

```powershell
python -m pytest tests -q
python -m compileall -q app tests
.\build_manager.ps1 -SkipInstaller
```

## Shared contract

`document_types.json` is the versioned 104-type contract shared with Hoso Digitization Pipeline. The applications communicate through filesystem-compatible names and metadata, never Python imports.

## Publication hygiene

This public repository excludes production databases, PDFs, images, logs, runtime outputs, private fixtures, and operator configuration. No open-source license is granted by this publication; all rights are reserved.