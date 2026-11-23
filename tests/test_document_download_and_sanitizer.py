"""
文档上传校验、受控网关下载响应头（RFC 5987 / 206 句柄关闭 / X-Accel-Redirect）
与 HTML 净化器受控 download 属性放行的离线回归测试。
"""

import io
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import RequestFactory, SimpleTestCase, override_settings

from content_blocks.sanitizer import sanitize_html
from media_library.delivery import serve_media_asset
from media_library.forms import validate_document_upload


class _TrackingFile:
    """带关闭计数跟踪的内存二进制流，用于验证 206/416 分段路径不会泄露句柄。"""

    def __init__(self, payload: bytes, name: str):
        self._payload = payload
        self.name = name
        self.size = len(payload)
        self._buf = io.BytesIO(payload)
        self.closed = False

    def open(self, mode="rb"):
        self.closed = False
        self._buf = io.BytesIO(self._payload)
        return self

    def seek(self, pos: int, whence: int = io.SEEK_SET):
        return self._buf.seek(pos, whence)

    def tell(self):
        return self._buf.tell()

    def read(self, length: int = -1) -> bytes:
        return self._buf.read(length)

    def close(self):
        self.closed = True
        self._buf.close()


class DocumentDownloadAndSanitizerTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_settings_and_validate_document_upload_allow_md_and_50mib(self):
        self.assertIn("md", settings.WAGTAILDOCS_EXTENSIONS)
        self.assertIn("markdown", settings.WAGTAILDOCS_EXTENSIONS)
        self.assertEqual(settings.WAGTAILDOCS_MAX_UPLOAD_SIZE, 50 * 1024 * 1024)
        self.assertGreaterEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 20 * 1024 * 1024)

        ok_md = ContentFile(b"# Hello", name="课程讲义.MD")
        self.assertIs(validate_document_upload(ok_md), ok_md)

        empty_md = ContentFile(b"", name="empty.md")
        with self.assertRaises(ValidationError):
            validate_document_upload(empty_md)

        bad_exe = ContentFile(b"MZ", name="payload.exe")
        with self.assertRaises(ValidationError):
            validate_document_upload(bad_exe)

        oversized = SimpleNamespace(name="huge.zip", size=50 * 1024 * 1024 + 1)
        with self.assertRaises(ValidationError):
            validate_document_upload(oversized)

    def test_document_gateway_sets_rfc5987_attachment_headers_for_get_head_206_416_and_accel(self):
        doc_uuid = uuid4()
        payload = b"# Course Notes\n" + b"0123456789" * 5
        tracked = _TrackingFile(payload, name="documents/课程 笔记_abc123.md")
        fake_doc = SimpleNamespace(
            public_id=doc_uuid,
            title="课程笔记",
            filename="课程 笔记.md",
            visibility="public",
            file=tracked,
        )

        with mock.patch("media_library.delivery.get_media_asset_by_id", return_value=fake_doc), \
             mock.patch("media_library.delivery.check_media_access", return_value=True):
            # 1. 普通 GET
            req_get = self.factory.get(f"/media-gateway/document/{doc_uuid}/")
            res_get = serve_media_asset(req_get, "document", doc_uuid)
            self.assertEqual(res_get.status_code, 200)
            self.assertIn("attachment;", res_get["Content-Disposition"])
            self.assertIn("filename*=UTF-8''%E8%AF%BE%E7%A8%8B%20%E7%AC%94%E8%AE%B0.md", res_get["Content-Disposition"])
            self.assertEqual(res_get["X-Content-Type-Options"], "nosniff")
            self.assertEqual(res_get["Content-Security-Policy"], "default-src 'none'")
            self.assertEqual(res_get["Cache-Control"], "private, no-store")
            res_get.close()

            # 2. HEAD
            req_head = self.factory.head(f"/media-gateway/document/{doc_uuid}/")
            res_head = serve_media_asset(req_head, "document", doc_uuid)
            self.assertEqual(res_head.status_code, 200)
            self.assertEqual(res_head["Content-Length"], str(len(payload)))
            self.assertIn("attachment;", res_head["Content-Disposition"])
            self.assertEqual(res_head.content, b"")

            # 3. 206 Range：验证关闭文件流 + 补齐安全头与下载头
            req_206 = self.factory.get(f"/media-gateway/document/{doc_uuid}/", HTTP_RANGE="bytes=0-9")
            res_206 = serve_media_asset(req_206, "document", doc_uuid)
            self.assertEqual(res_206.status_code, 206)
            self.assertTrue(tracked.closed, "206 分段响应后必须关闭底层文件流")
            self.assertEqual(res_206["X-Content-Type-Options"], "nosniff")
            self.assertEqual(res_206["Content-Security-Policy"], "default-src 'none'")
            self.assertIn("attachment;", res_206["Content-Disposition"])

            # 4. 416 Range 越界：同样关闭文件流
            req_416 = self.factory.get(f"/media-gateway/document/{doc_uuid}/", HTTP_RANGE="bytes=9999-")
            res_416 = serve_media_asset(req_416, "document", doc_uuid)
            self.assertEqual(res_416.status_code, 416)
            self.assertTrue(tracked.closed)

            # 5. X-Accel-Redirect 模式：中文与空格路径需百分号编码，且附带下载头
            with override_settings(USE_X_ACCEL_REDIRECT=True):
                res_accel = serve_media_asset(req_get, "document", doc_uuid)
                self.assertEqual(res_accel.status_code, 200)
                self.assertEqual(
                    res_accel["X-Accel-Redirect"],
                    "/internal-rustfs/documents/%E8%AF%BE%E7%A8%8B%20%E7%AC%94%E8%AE%B0_abc123.md",
                )
                self.assertIn("attachment;", res_accel["Content-Disposition"])
                self.assertEqual(res_accel["Content-Security-Policy"], "default-src 'none'")

    def test_sanitizer_preserves_download_only_for_document_gateway_links(self):
        doc_uuid = uuid4()
        internal_html = (
            f'<div class="media-document-card"><a href="/media-gateway/document/{doc_uuid}/" '
            f'class="doc-download-link" download>课件.zip</a></div>'
        )
        cleaned_internal = sanitize_html(internal_html)
        self.assertIn("download", cleaned_internal)
        self.assertIn('class="doc-download-link"', cleaned_internal)

        external_html = '<a href="https://evil.example/a.exe" class="doc-download-link" download="hack.exe">外链</a>'
        cleaned_external = sanitize_html(external_html)
        self.assertNotIn("download", cleaned_external)
        self.assertNotIn("doc-download-link", cleaned_external)
        self.assertIn('rel="noopener noreferrer"', cleaned_external)
