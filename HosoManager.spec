# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
PROVENANCE = Path(os.environ["HOSO_BUILD_PROVENANCE"])
if not PROVENANCE.is_file():
    raise SystemExit("HOSO_BUILD_PROVENANCE must identify an existing provenance JSON")
BUNDLE_NAME = os.environ.get("HOSO_BUNDLE_NAME", "HosoManager")
hidden = collect_submodules("app.manager")
a = Analysis(
    [str(ROOT / "app" / "manager" / "entrypoint.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "document_types.json"), "."),
        (str(PROVENANCE), "."),
        (str(ROOT / "app" / "manager" / "templates"), "app/manager/templates"),
        (str(ROOT / "app" / "manager" / "static"), "app/manager/static"),
    ],
    hiddenimports=hidden + ["pypdf"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=BUNDLE_NAME, debug=False, bootloader_ignore_signals=False, strip=False, upx=True, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name=BUNDLE_NAME)
