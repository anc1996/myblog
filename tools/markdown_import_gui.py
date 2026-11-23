"""
Windows 桌面导入向导可执行程序入口（供 PyInstaller 打包）。
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "myblog" / "apps"
for p in (str(ROOT), str(APPS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tools.markdown_import.gui import main

if __name__ == "__main__":
    main()
