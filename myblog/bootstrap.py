"""
myblog.bootstrap
统一 Python 路径自举与 apps/ 目录注入服务。

无论从哪个入口（manage.py、wsgi.py、asgi.py、settings、单测或运维脚本）执行，
均确保：
1. BASE_DIR 位于 sys.path 中；
2. APPS_DIR (myblog/myblog/apps) 位于 sys.path[0] 中，确保所有子应用均以顶层名称直接导入。
"""

import sys
from pathlib import Path


def configure_app_imports() -> Path:
    project_dir = Path(__file__).resolve().parent
    base_dir = project_dir.parent
    apps_dir = project_dir / "apps"

    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))

    if str(apps_dir) not in sys.path:
        sys.path.insert(0, str(apps_dir))

    return apps_dir
