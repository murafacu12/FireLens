# PyInstaller recipe for the FIRELENS desktop app (Windows).
# Build (from backend/, after `npm run build` in frontend/):
#     .venv\Scripts\python -m PyInstaller firelens.spec --noconfirm
# Output: backend/dist/FIRELENS/FIRELENS.exe (a folder app; see desktop/build.ps1 for the installer)
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = Path(SPECPATH)
WEB = HERE.parent / "frontend" / "dist"
if not (WEB / "index.html").is_file():
    raise SystemExit("Build the website first: cd frontend && npm run build")

datas = [(str(WEB), "web")]
binaries = []
hiddenimports = collect_submodules("app") + collect_submodules("uvicorn") + ["sqlalchemy.dialects.sqlite"]

# rasterio ships GDAL and PROJ (DLLs + gdal_data/proj_data); take all of it
for pkg in ("rasterio", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["desktop_entry.py"],
    pathex=[str(HERE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "sklearn", "joblib", "matplotlib", "pytest", "respx", "IPython", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FIRELENS",
    icon=str(HERE / "desktop" / "firelens.ico"),
    console=True,  # the window shows the address and status; closing it stops FIRELENS
    upx=False,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="FIRELENS", upx=False)
