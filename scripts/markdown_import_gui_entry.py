"""
Windows 桌面导入向导 PyInstaller 打包入口。
置于 scripts/ 下以避免与 tools/markdown_import 和 myblog/apps/markdown_import 同名包遮蔽。
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
