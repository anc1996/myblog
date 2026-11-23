from datetime import date
import unittest

from markdown_import import (
    DraftSpec,
    ImportSchema,
    clear_import_adapters,
    get_import_adapter,
    list_import_adapters,
    register_import_adapter,
)
from markdown_import.parser import (
    align_tags_with_vocabulary,
    extract_local_media_refs,
    extract_title_and_strip_h1,
    fallback_title_from_filename,
    normalize_tags_input,
    parse_authored_date,
    parse_explicit_steps,
    parse_front_matter,
    rewrite_asset_links,
    validate_relative_asset_path,
)


class _DummyAdapter:
    type_key = "demo"
    model_label = "demo.DemoPage"
    schema_version = 1

    def describe(self, actor=None, parent=None):
        return ImportSchema(
            type_key="demo",
            label="Demo",
            model_label="demo.DemoPage",
            schema_version=1,
            fields=(),
            allowed_parent_models=("home.HomePage",),
        )

    def normalize(self, parsed_doc, edits, context):
        return DraftSpec(type_key="demo", title="t", slug_hint="t", authored_on=date.today())

    def build(self, draft_spec, resolved_assets):
        return None

    def publish_warnings(self, draft_spec):
        return []


class MarkdownImportParserAndRegistryTests(unittest.TestCase):
    def setUp(self):
        from markdown_import.registry import _IMPORT_ADAPTERS
        self._saved_adapters = dict(_IMPORT_ADAPTERS)

    def tearDown(self):
        from markdown_import.registry import _IMPORT_ADAPTERS
        _IMPORT_ADAPTERS.clear()
        _IMPORT_ADAPTERS.update(self._saved_adapters)

    def test_registry_prevents_empty_or_duplicate_keys(self):
        clear_import_adapters()
        adapter = _DummyAdapter()
        register_import_adapter(adapter)
        self.assertIs(get_import_adapter("demo"), adapter)
        self.assertEqual(len(list_import_adapters()), 1)
        with self.assertRaises(ValueError):
            register_import_adapter(adapter)

    def test_front_matter_separator_vs_unclosed_vs_duplicate_key(self):
        plain = "这是正文第一段\n\n---\n\n这是分隔线后面的段落"
        fm, body, issues = parse_front_matter(plain)
        self.assertEqual(fm, {})
        self.assertEqual(issues, [])
        self.assertIn("分隔线后面的段落", body)

        unclosed = "---\ntitle: 未闭合\n\n这里是重要正文"
        _, body2, issues2 = parse_front_matter(unclosed)
        self.assertEqual(issues2[0].code, "front_matter_unclosed")
        self.assertIn("这里是重要正文", body2)

        dup = "---\ntitle: A\ntitle: B\n---\n正文"
        _, _, issues3 = parse_front_matter(dup)
        self.assertEqual(issues3[0].code, "front_matter_duplicate_key")

    def test_title_h1_extraction_ignores_code_fence_and_preserves_mid_h1(self):
        md_code_first = "```python\n# 这是Python注释不是标题\nx = 1\n```\n\n正文内容"
        title, source, stripped = extract_title_and_strip_h1({}, md_code_first, "01-test-post.md")
        self.assertEqual(title, "")
        self.assertEqual(source, "missing")
        self.assertIn("# 这是Python注释不是标题", stripped)
        self.assertEqual(fallback_title_from_filename("2026-11-22-01-wagtail_guide.md"), "wagtail guide")

        md_multi_h1 = "# 总标题\n\n开篇段落。\n\n# 第二章 正文保留\n\n内容。"
        title2, source2, stripped2 = extract_title_and_strip_h1({}, md_multi_h1, "a.md")
        self.assertEqual(title2, "总标题")
        self.assertEqual(source2, "h1")
        self.assertNotIn("# 总标题", stripped2)
        self.assertIn("# 第二章 正文保留", stripped2)

    def test_date_and_tag_normalization(self):
        default_d = date(2026, 11, 22)
        self.assertEqual(parse_authored_date(None, default_d), (default_d, []))
        self.assertEqual(parse_authored_date("2026/11/05", default_d)[0], date(2026, 11, 5))
        self.assertEqual(parse_authored_date("2026.11.06", default_d)[0], date(2026, 11, 6))
        _, errs = parse_authored_date("2026-02-30", default_d)
        self.assertEqual(errs[0].code, "authored_date_invalid")

        tags = normalize_tags_input("PostgreSQL， wagtail、Python, postgresql")
        self.assertEqual(tags, ["PostgreSQL", "wagtail", "Python"])
        aligned, unmatched = align_tags_with_vocabulary(tags, ["Wagtail", "PostgreSQL", "Django"])
        self.assertEqual(aligned, ["PostgreSQL", "Wagtail"])
        self.assertEqual(unmatched, ["Python"])

    def test_explicit_step_blocks_and_ignored_h2(self):
        md = "## 普通二级标题\n\n说明\n\n:::step 安装依赖\npip install django\n:::\n\n```md\n:::step 代码块内忽略\n:::\n```"
        steps, rem, issues = parse_explicit_steps(md)
        self.assertEqual(issues, [])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step_number"], "1")
        self.assertEqual(steps[0]["title"], "安装依赖")
        self.assertIn("## 普通二级标题", rem)
        self.assertIn(":::step 代码块内忽略", rem)

    def test_asset_path_security_and_link_rewrite_skips_code(self):
        for bad in (
            "../secret.png",
            "images/%2e%2e/secret.png",
            "/etc/passwd",
            "C:\\Windows\\win.ini",
            "\\\\server\\share\\a.png",
            "file:///tmp/a.png",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_relative_asset_path(bad)

        md = (
            "![图1](images/arch.png)\n\n"
            "[下载课件](files/lab.zip)\n\n"
            "`![示例](images/arch.png)`\n\n"
            "```markdown\n![代码块内](images/arch.png)\n```\n"
        )
        refs, issues = extract_local_media_refs(md, {"cover": "images/cover.jpg"})
        self.assertEqual(issues, [])
        self.assertEqual([(r.source_path, r.media_type) for r in refs], [
            ("images/cover.jpg", "image"),
            ("images/arch.png", "image"),
            ("files/lab.zip", "document"),
        ])

        rewritten = rewrite_asset_links(
            md,
            {
                "images/arch.png": ("image", "11111111-1111-1111-1111-111111111111"),
                "files/lab.zip": ("document", "22222222-2222-2222-2222-222222222222"),
            },
        )
        self.assertIn("![图1](asset://image/11111111-1111-1111-1111-111111111111)", rewritten)
        self.assertIn("[下载课件](asset://document/22222222-2222-2222-2222-222222222222)", rewritten)
        self.assertIn("`![示例](images/arch.png)`", rewritten)
        self.assertIn("![代码块内](images/arch.png)", rewritten)
