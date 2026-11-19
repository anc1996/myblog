"""
第二批（选项 A）：Wagtail 8.1 后台左右分栏编辑器（Vditor + Mermaid + 复杂表格预览 + 粘贴传图）测试。
"""

import io
import json
from PIL import Image
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from wagtail.admin.telepath import JSContext
from wagtail.models import Collection, GroupCollectionPermission

from content_blocks.blocks.markdown import CustomMarkdownBlock
from content_blocks.blocks.mermaid import CustomMermaidBlock
from content_blocks.blocks.table import CustomTableBlock
from content_blocks.widgets import (
    MermaidSplitWidget,
    TableJsonSplitWidget,
    VditorMarkdownWidget,
)
from content_lifecycle.permissions import ensure_author_role_infrastructure
from media_library.models import CustomImage, VisibilityChoices

User = get_user_model()


def _make_png_upload(name="paste.png", size=(32, 32)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=(20, 120, 220)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class AdminSplitEditorsTests(TestCase):

    def test_block_widgets_and_telepath_registration(self):
        """验证三大积木均装配对应后台分栏控件且 Telepath 序列化正常"""
        md_block = CustomMarkdownBlock()
        self.assertIsInstance(md_block.field.widget, VditorMarkdownWidget)
        self.assertFalse(md_block.field.strip)
        md_html = md_block.field.widget.render("body-0-value", "  # keep indent\n")
        self.assertIn('data-cb-vditor-markdown="true"', md_html)
        self.assertIn('data-vditor-mode="sv"', md_html)
        self.assertIn(reverse("content_blocks_vditor_image_upload"), md_html)

        mermaid_block = CustomMermaidBlock()
        m_widget = mermaid_block.child_blocks["diagram_code"].field.widget
        self.assertIsInstance(m_widget, MermaidSplitWidget)
        m_html = m_widget.render("diagram_code", "graph TD; A-->B;")
        self.assertIn("data-cb-admin-mermaid-field", m_html)
        self.assertIn("data-cb-admin-mermaid-theme-label", m_html)

        table_block = CustomTableBlock()
        t_widget = table_block.child_blocks["table_data"].field.widget
        self.assertIsInstance(t_widget, TableJsonSplitWidget)
        t_html = t_widget.render("table_data", '{"rows": []}')
        self.assertIn("data-cb-admin-table-field", t_html)
        self.assertIn("data-cb-table-sample-btn", t_html)
        self.assertIn(reverse("content_blocks_table_preview"), t_html)

        js_ctx = JSContext()
        packed = js_ctx.pack([md_block.field.widget, m_widget, t_widget])
        packed_json = json.dumps(packed)
        self.assertIn("content_blocks.widgets.VditorMarkdownWidget", packed_json)
        self.assertIn("content_blocks.widgets.MermaidSplitWidget", packed_json)
        self.assertIn("content_blocks.widgets.TableJsonSplitWidget", packed_json)

    def test_vditor_image_upload_permission_and_author_isolation(self):
        """验证 Vditor 粘贴传图：匿名拦截、无权拦截、受限作者上传自动绑定 RESTRICTED 与工作区集合"""
        url = reverse("content_blocks_vditor_image_upload")

        # 1. 未登录拦截
        resp_anon = self.client.post(url, {"file": _make_png_upload()})
        self.assertIn(resp_anon.status_code, (302, 403))

        # 2. 有后台登录权但无图片 add 权的普通编辑被 403 拒绝
        no_img_user = User.objects.create_user(username="noimg_editor", password="Password123!")
        access_perm = Permission.objects.get(codename="access_admin")
        no_img_user.user_permissions.add(access_perm)
        self.client.force_login(no_img_user)
        resp_no_perm = self.client.post(url, {"file": _make_png_upload()})
        self.assertEqual(resp_no_perm.status_code, 403)

        # 3. Authors 受限作者上传成功：落入 Author Workspace、visibility=RESTRICTED、uploaded_by_user=本人
        infra = ensure_author_role_infrastructure()
        author = User.objects.create_user(username="vditor_author", password="Password123!")
        author.groups.add(infra["authors_group"])
        self.client.force_login(author)

        resp_ok = self.client.post(url, {"file": _make_png_upload("arch-shot.png")})
        self.assertEqual(resp_ok.status_code, 201)
        data = resp_ok.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["markdown"].startswith("![arch-shot](asset://image/"))
        self.assertEqual(data["preview_url"], f"/media-gateway/image/{data['asset_id']}/")

        img = CustomImage.objects.get(public_id=data["asset_id"])
        self.assertEqual(img.uploaded_by_user_id, author.id)
        self.assertEqual(img.visibility, VisibilityChoices.RESTRICTED)
        self.assertEqual(img.collection_id, infra["workspace_collection"].id)

        # 4. 非法图片内容被拒绝
        bad_file = SimpleUploadedFile("bad.png", b"not-an-image", content_type="image/png")
        resp_bad = self.client.post(url, {"file": bad_file})
        self.assertEqual(resp_bad.status_code, 400)

    def test_table_live_preview_endpoint(self):
        """验证复杂表格后台实时预览接口：返回跨行/公式/代码 HTML，非法网格返回 400"""
        url = reverse("content_blocks_table_preview")
        resp_anon = self.client.post(url, data=json.dumps({}), content_type="application/json")
        self.assertIn(resp_anon.status_code, (302, 403))

        infra = ensure_author_role_infrastructure()
        author = User.objects.create_user(username="tbl_preview_author", password="Password123!")
        author.groups.add(infra["authors_group"])
        self.client.force_login(author)

        valid_payload = {
            "caption": "后台实时预览测试表",
            "has_header_row": True,
            "has_header_col": False,
            "alignment": "center",
            "table_data": json.dumps({
                "rows": [
                    [{"value": "模块", "rowspan": 2, "header": True}, {"value": "指标", "colspan": 2, "header": True}],
                    [{"value": "公式", "header": True}, {"value": "代码", "header": True}],
                    [{"value": "![主库图|240](asset://image/11111111-1111-1111-1111-111111111111)", "rowspan": 2}, "$E=mc^2$", "`SELECT 1`"],
                    ["$t \\le 2$", "```sql\nSELECT 1;\n```"],
                ]
            }),
        }
        resp_ok = self.client.post(url, data=json.dumps(valid_payload), content_type="application/json")
        self.assertEqual(resp_ok.status_code, 200)
        body_ok = resp_ok.json()
        self.assertTrue(body_ok["ok"])
        self.assertIn('class="responsive-table-wrapper"', body_ok["html"])
        self.assertIn('rowspan="2"', body_ok["html"])
        self.assertIn('width="240"', body_ok["html"])
        self.assertIn('/media-gateway/image/11111111-1111-1111-1111-111111111111/', body_ok["html"])
        self.assertIn('data-cb-math="inline"', body_ok["html"])
        self.assertIn('<code class="cb-inline-code">SELECT 1</code>', body_ok["html"])

        bad_payload = {
            "table_data": json.dumps({"rows": [[{"value": "A", "rowspan": 3}], [{"value": "B"}]]}),
        }
        resp_err = self.client.post(url, data=json.dumps(bad_payload), content_type="application/json")
        self.assertEqual(resp_err.status_code, 400)
        body_err = resp_err.json()
        self.assertFalse(body_err["ok"])
        self.assertTrue(len(body_err["errors"]) > 0)
