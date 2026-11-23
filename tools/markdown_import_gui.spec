# PyInstaller spec for myblog multi-page Markdown importer GUI.
from pathlib import Path

ROOT = Path(SPECPATH).resolve()
PROJECT_ROOT = ROOT.parent
APPS_DIR = PROJECT_ROOT / "myblog" / "apps"

hiddenimports = [
    "tools.markdown_import",
    "tools.markdown_import.client",
    "tools.markdown_import.scanner",
    "tools.markdown_import.templates",
    "tools.markdown_import.gui",
    "markdown_import",
    "markdown_import.contracts",
    "markdown_import.parser",
    "yaml",
    "markdown_it",
    "requests",
]

a = Analysis(
    [str(PROJECT_ROOT / "scripts" / "markdown_import_gui_entry.py")],
    pathex=[str(APPS_DIR), str(PROJECT_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["django", "wagtail", "pymongo", "psycopg", "boto3", "redis", "jieba"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="myblog-markdown-importer-v2",
    # 若旧版 exe 正在运行被占用，先输出新版本或覆盖；若无占用仍叫 myblog-markdown-importer
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
