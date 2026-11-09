"""
myblog.apps
全站业务子应用物理收敛目录容器。
子应用通过 myblog.bootstrap.configure_app_imports() 注入 sys.path[0]，以顶层模块名直接导入。
严禁在本文件中导入任何子应用模块，杜绝双重命名空间导入。
"""
