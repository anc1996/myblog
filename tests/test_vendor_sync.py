"""
AV-1 Vendor 同步与生产 ManifestStaticFilesStorage 兼容测试。
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts import sync_vendor_assets


def _make_fake_tgz(dest: Path, files: dict[str, bytes]) -> tuple[str, str]:
    import base64
    import hashlib

    with tarfile.open(dest, "w:gz") as tf:
        for rel, content in files.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))

    raw = dest.read_bytes()
    sha512_b64 = base64.b64encode(hashlib.sha512(raw).digest()).decode("ascii")
    sha1_hex = hashlib.sha1(raw).hexdigest()
    return sha512_b64, sha1_hex


class VendorSyncTests(unittest.TestCase):
    def test_videojs_entry_in_vendor_lock(self):
        lock = sync_vendor_assets.load_lock(sync_vendor_assets.DEFAULT_LOCK_FILE)
        packages = lock.get("packages", {})
        self.assertIn("videojs", packages)
        vjs = packages["videojs"]
        self.assertEqual(vjs.get("npm_package"), "@videojs/cdn")
        self.assertEqual(vjs.get("version"), "10.5.0")
        self.assertEqual(vjs.get("license"), "Apache-2.0")
        self.assertTrue(vjs.get("strip_missing_sourcemaps"))

    def test_sync_strips_missing_sourcemaps_excludes_dev_files_and_fixes_vditor_fonts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cache_dir = tmp_path / "cache"
            cache_dir.mkdir()
            target_root = tmp_path / "vendor"

            vditor_tgz = cache_dir / "vditor-3.11.2.tgz"
            v_sha512, v_sha1 = _make_fake_tgz(
                vditor_tgz,
                {
                    "package/LICENSE": b"MIT",
                    "package/dist/index.min.js": b"console.log(1);\n//# sourceMappingURL=index.min.js.map\n",
                    "package/dist/js/mathjax/sre/sre-node.js": b"var a=1;\n//# sourceMappingURL=sre-node.js.map\n",
                    "package/dist/js/katex/fonts/KaTeX_AMS-Regular.woff2": b"woff2",
                    "package/dist/js/markmap/katex.min.css": b"@font-face{src:url(fonts/KaTeX_AMS-Regular.woff2)}",
                },
            )

            vjs_tgz = cache_dir / "videojs-10.5.0.tgz"
            vjs_sha512, vjs_sha1 = _make_fake_tgz(
                vjs_tgz,
                {
                    "package/LICENSE": b"Apache-2.0",
                    "package/video.js": b"import './chunk-1.js';\n//# sourceMappingURL=video.js.map\n",
                    "package/video.js.map": b"{}",
                    "package/video.dev.js": b"// dev",
                    "package/video.d.ts": b"export {};",
                    "package/audio.js": b"import './chunk-1.js';\n//# sourceMappingURL=audio.js.map\n",
                    "package/chunk-1.js": b"export const ok = 1;\n",
                    "package/global.css": b"video-player{display:contents}",
                    "package/locales/zh-CN.js": b"export default {};\n",
                },
            )

            lock_file = tmp_path / "vendor.lock.json"
            lock_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "target_root": str(target_root),
                        "packages": {
                            "vditor": {
                                "version": "3.11.2",
                                "registry_url": "https://registry.npmjs.org/vditor/-/vditor-3.11.2.tgz",
                                "integrity_sha512": v_sha512,
                                "shasum_sha1": v_sha1,
                                "license": "MIT",
                                "strip_missing_sourcemaps": True,
                                "fix_vditor_markmap_katex_fonts": True,
                                "extract": [
                                    {"src": "package/dist/", "dest": "dist/", "recursive": True},
                                    {"src": "package/LICENSE", "dest": "LICENSE"},
                                ],
                            },
                            "videojs": {
                                "npm_package": "@videojs/cdn",
                                "version": "10.5.0",
                                "registry_url": "https://registry.npmjs.org/@videojs/cdn/-/cdn-10.5.0.tgz",
                                "integrity_sha512": vjs_sha512,
                                "shasum_sha1": vjs_sha1,
                                "license": "Apache-2.0",
                                "strip_missing_sourcemaps": True,
                                "extract": [
                                    {
                                        "src": "package/",
                                        "dest": ".",
                                        "recursive": True,
                                        "exclude_suffixes": [".map", ".d.ts", ".ts", ".dev.js"],
                                    },
                                    {"src": "package/video.js", "dest": "video.js"},
                                    {"src": "package/audio.js", "dest": "audio.js"},
                                    {"src": "package/global.css", "dest": "global.css"},
                                    {"src": "package/locales/zh-CN.js", "dest": "locales/zh-CN.js"},
                                    {"src": "package/LICENSE", "dest": "LICENSE"},
                                ],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            rc_sync = sync_vendor_assets.cmd_sync(
                lock_file, force=True, dry_run=False, cache_dir=cache_dir
            )
            self.assertEqual(rc_sync, 0)

            # 1. Vditor 缺失 sourcemap 已被移除且 markmap/katex.min.css 字体路径被修正
            sre_text = (target_root / "vditor" / "dist" / "js" / "mathjax" / "sre" / "sre-node.js").read_text()
            self.assertNotIn("sourceMappingURL=", sre_text)
            mm_css = (target_root / "vditor" / "dist" / "js" / "markmap" / "katex.min.css").read_text()
            self.assertIn("url(../katex/fonts/KaTeX_AMS-Regular.woff2)", mm_css)
            self.assertNotIn("url(fonts/", mm_css)

            # 2. Video.js 排除了 .map/.d.ts/.dev.js 且清除了缺失 sourcemap
            vjs_dir = target_root / "videojs"
            self.assertTrue((vjs_dir / "video.js").is_file())
            self.assertTrue((vjs_dir / "audio.js").is_file())
            self.assertTrue((vjs_dir / "chunk-1.js").is_file())
            self.assertFalse((vjs_dir / "video.js.map").exists())
            self.assertFalse((vjs_dir / "video.dev.js").exists())
            self.assertFalse((vjs_dir / "video.d.ts").exists())
            self.assertNotIn("sourceMappingURL=", (vjs_dir / "video.js").read_text())

            # 3. check 校验通过
            self.assertEqual(sync_vendor_assets.cmd_check(lock_file), 0)
            manifest_path = target_root / "vditor" / sync_vendor_assets.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("install_rules_sha256", None)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(sync_vendor_assets.cmd_check(lock_file), 1)
            self.assertEqual(
                sync_vendor_assets.cmd_sync(lock_file, force=False, dry_run=False, cache_dir=cache_dir),
                0,
            )
            self.assertEqual(sync_vendor_assets.cmd_check(lock_file), 0)
