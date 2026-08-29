# HosoManager Local Packaging

This document describes the packaging implementation for the accepted
`0.2.1-rc1` Windows RC. It does not change the application or release policy.

## Prerequisites

- Windows 10/11. The accepted RC was built and exercised on Windows 11,
  build 26200.
- Python available as `python` on `PATH`.
- Dependencies from `requirements.txt`, including PyInstaller and pytest.
- Inno Setup 6 is optional for the installer build. It was not available for
  the accepted RC, so installer acceptance remains pending.

Install the Python dependencies with:

```powershell
python -m pip install -r requirements.txt
```

## Official build command

The build script enforces the source version `0.2.1-rc1`, refuses a dirty
working tree, records the exact Git HEAD, and embeds build provenance.

Portable/onedir RC bundle:

```powershell
.\build_manager.ps1 -SkipInstaller
```

If Inno Setup 6 is installed, the same script can also build the installer:

```powershell
.\build_manager.ps1
```

Use `-IsccPath` when `ISCC.exe` is not in the default Inno Setup location.

## Artifact layout

PyInstaller uses `COLLECT`, so the output is an onedir bundle, not a onefile
executable:

```text
dist/
└── HosoManager-v0.2.1-rc1/
    ├── HosoManager-v0.2.1-rc1.exe
    ├── config.example.json
    └── _internal/
        └── build_provenance.json
```

The accepted Windows RC artifact is:

```text
dist/HosoManager-v0.2.1-rc1/HosoManager-v0.2.1-rc1.exe
```

Do not rename an RC binary and present it as a production version. A
production release requires its own version and a separately built artifact.

## Version and provenance

The build script accepts only the source version `0.2.1-rc1` for this RC. It
writes a temporary provenance record containing the version, exact Git SHA,
UTC build timestamp, Python version, and PyInstaller version. The spec embeds
that record under `_internal/build_provenance.json`.

The accepted RC provenance is recorded in:

```text
docs/release-provenance-0.2.1-rc1.json
```

## Configuration and data

For a frozen build, `config.json` is read from the directory beside the EXE.
If it does not exist, the application creates it with these portable defaults:

- data root: `done/output` beside the EXE;
- metadata database: `data/manager.db` beside the EXE;
- host: `127.0.0.1`;
- port: `8765`;
- browser-on-start: enabled for the first-run bootstrap configuration.

Relative paths are resolved relative to the configuration file. Operators may
configure a different local data root through Settings. Runtime configuration,
SQLite files, PDFs, logs, and other operator data are not release artifacts and
must not be committed.

## Runtime contract

- The Manager binds only to localhost (`127.0.0.1` or `localhost`).
- It is offline and does not upload data or depend on the Pipeline repository
  at runtime.
- `/health` is served on the configured localhost port.
- SQLite stores index and metadata; PDFs remain on the filesystem.
- Scanning, listing, viewing, and metadata operations must not rename, move,
  delete, overwrite, or modify source PDFs. Manual document addition creates
  new output files under the configured data root.

## Installer status

`installer/HosoManager.iss` exists, but Inno Setup was unavailable during the
accepted Windows RC. Therefore:

```text
INSTALLER_ACCEPTANCE = PENDING
```

The installer has not been claimed as tested. Its current shortcut entries
target `HosoManager.exe`, while the accepted onedir bundle contains the
versioned filename `HosoManager-v0.2.1-rc1.exe`; installer packaging must be
corrected and acceptance-tested before an installer release is recommended.
