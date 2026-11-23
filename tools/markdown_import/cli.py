"""
命令行导入客户端 (CLI)：支持配对换令牌、预检、一键导入未发布草稿与模板输出。

示例：
  python -m tools.markdown_import.cli template blog_page
  python -m tools.markdown_import.cli pair --url http://192.168.20.5:8000 --code PAIR-XXXX-YYYY
  python -m tools.markdown_import.cli import --url http://192.168.20.5:8000 --token mdimp_... --type blog_page --parent 3 ./post/demo.md
"""

import argparse
import json
import sys

from .client import MarkdownImportClient
from .scanner import scan_markdown_source
from .templates import render_front_matter_template


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="myblog 通用多页面 Markdown 导入命令行工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tpl = sub.add_parser("template", help="打印指定页面类型的标准 YAML Front Matter 模板")
    p_tpl.add_argument("type_key", choices=["blog_page", "guide_page", "project_page"])

    p_pair = sub.add_parser("pair", help="使用后台一次性配对码兑换导入令牌")
    p_pair.add_argument("--url", required=True)
    p_pair.add_argument("--code", required=True)
    p_pair.add_argument("--allow-insecure-http", action="store_true", help="仅内网开发测试允许非本机 HTTP")

    p_imp = sub.add_parser("import", help="预检并导入本地 Markdown 为未发布草稿")
    p_imp.add_argument("source", help="本地 .md 文件路径")
    p_imp.add_argument("--root", default="", help="资料根目录（默认取 .md 所在目录）")
    p_imp.add_argument("--url", required=True)
    p_imp.add_argument("--token", required=True)
    p_imp.add_argument("--type", required=True, dest="type_key")
    p_imp.add_argument("--parent", required=True, type=int)
    p_imp.add_argument("--ai", action="store_true", help="无标题/简介/标签时尝试调用服务端 AI 补全")
    p_imp.add_argument("--allow-insecure-http", action="store_true", help="仅内网开发测试允许非本机 HTTP")

    args = parser.parse_args(argv)

    if args.command == "template":
        print(render_front_matter_template(args.type_key))
        return 0

    if args.command == "pair":
        client = MarkdownImportClient(args.url, allow_insecure_http=args.allow_insecure_http)
        res = client.exchange_pair_code(args.code)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.command == "import":
        client = MarkdownImportClient(args.url, args.token, allow_insecure_http=args.allow_insecure_http)
        scanned = scan_markdown_source(args.source, root_dir=args.root or None)
        edits = {}
        if args.ai:
            sug = client.ai_suggest(
                type_key=args.type_key,
                parent_id=args.parent,
                source_name=scanned.source_name,
                markdown_text=scanned.raw_text,
                allow_external_processing=True,
            )
            s = sug.get("suggestions") or {}
            if s.get("title"):
                edits["title"] = s["title"]
            if s.get("summary"):
                edits["intro" if args.type_key == "blog_page" else "summary"] = s["summary"]
            if s.get("tags"):
                edits["tags"] = s["tags"]
        res = client.import_source(
            type_key=args.type_key,
            parent_id=args.parent,
            scanned=scanned,
            edits=edits,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
