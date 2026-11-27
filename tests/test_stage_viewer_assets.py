"""
公共沉浸式画布查看器 (CBStageViewer + vendor/panzoom) 契约与静态卫生回归测试。
"""

import json
from pathlib import Path
import re
import subprocess
import sys

from django.test import SimpleTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StageViewerAndVendorPanzoomTests(SimpleTestCase):
    def test_vendor_lock_contains_panzoom_and_sync_check_passes(self):
        lock_path = PROJECT_ROOT / "myblog/static/vendor.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        pkgs = lock.get("packages", {})
        self.assertIn("panzoom", pkgs)
        pz = pkgs["panzoom"]
        self.assertEqual(pz["npm_package"], "@panzoom/panzoom")
        self.assertEqual(pz["license"], "MIT")
        self.assertTrue(pz.get("integrity_sha512"))
        self.assertTrue(pz.get("shasum_sha1"))

        res = subprocess.run(
            [sys.executable, "-B", str(PROJECT_ROOT / "scripts/sync_vendor_assets.py"), "check"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stdout + "\n" + res.stderr)
        self.assertIn("panzoom@4.6.2: OK", res.stdout)

    def test_block_assets_register_stage_viewer_and_panzoom_lazy_url(self):
        css_tpl = (
            PROJECT_ROOT / "myblog/apps/content_blocks/templates/content_blocks/includes/_block_assets_css.html"
        ).read_text(encoding="utf-8")
        js_tpl = (
            PROJECT_ROOT / "myblog/apps/content_blocks/templates/content_blocks/includes/_block_assets_js.html"
        ).read_text(encoding="utf-8")

        self.assertIn("content_blocks/css/stage-viewer.css", css_tpl)
        self.assertIn("content_blocks/js/stage-viewer.js", js_tpl)
        self.assertIn("vendor/panzoom/panzoom.min.js", js_tpl)
        self.assertIn("data-panzoom-url=", js_tpl)

    def test_stage_viewer_css_respects_tokens_and_zero_transition_all(self):
        css_text = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/css/stage-viewer.css"
        ).read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", css_text), [])
        self.assertIsNone(re.search(r"transition\s*:\s*all\b", css_text, re.IGNORECASE))
        for token in ("--color-surface", "--color-border", "--color-primary", "--color-backdrop"):
            self.assertIn(token, css_text)

    def test_mermaid_and_images_delegate_to_shared_stage_viewer_contract(self):
        mermaid_js = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/js/mermaid-block.js"
        ).read_text(encoding="utf-8")
        for required in (
            "window.CBStageViewer",
            "data-mermaid-node-search",
            "panToElement",
            "exportMermaidPng2x",
            "PNG 2x",
            "复制源码",
        ):
            self.assertIn(required, mermaid_js)

        images_js = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/js/content-images.js"
        ).read_text(encoding="utf-8")
        for required in (
            "window.CBStageViewer",
            ".detail-body-section img",
            "enableFitWidth: true",
            "applyRotation",
            "cb-stage-thumb-btn",
        ):
            self.assertIn(required, images_js)
        # 按决策 A：图片灯箱不提供显式下载按钮
        self.assertNotIn('a.download', images_js)
        self.assertNotIn("下载图片", images_js)
