"""
通用多页面 Markdown 导入全链路集成测试（含权限、幂等、脱水与对账隔离）。
"""

import hashlib
import io
import json
from pathlib import Path
import tempfile
from unittest import mock
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from PIL import Image as PILImage
from wagtail.models import Page, PageViewRestriction, Site

from blog.models import BlogIndexPage, BlogPage
from content_lifecycle.permissions import ensure_author_role_infrastructure
from content_lifecycle.reconciliation import CrossStorageReconciler
from external_content.models import OperationResult
from guides.models import GuideIndexPage, GuidePage
from home.models import HomePage
from markdown_import.auth import issue_pair_code
from markdown_import.models import ImportAsset, ImportJob, ImportJobStatus, MarkdownImportToken
from media_library.models import CustomDocument, CustomImage, MediaReferenceRecord, VisibilityChoices
from people.models import Person
from portfolio.models import PortfolioIndexPage, ProjectPage
from tools.markdown_import import scan_markdown_source
from tools.markdown_import.cli import main as cli_main
from tools.markdown_import.client import MarkdownImportClient, load_session_checkpoint

User = get_user_model()


def _make_png_bytes() -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (24, 24), color=(15, 118, 110)).save(buf, format="PNG")
    return buf.getvalue()


class MarkdownImportIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_author_role_infrastructure()
        cls.site = Site.objects.filter(is_default_site=True).first() or Site.objects.first()
        cls.home = cls.site.root_page.specific if cls.site else HomePage.objects.first()
        if not cls.home:
            root = Page.get_first_root_node()
            cls.home = HomePage(title="Home", slug="home-test")
            root.add_child(instance=cls.home)
            cls.site = Site.objects.create(hostname="localhost", root_page=cls.home, is_default_site=True)

        cls.author = User.objects.create_user(username="md_author", password="PassWord12345!")
        cls.author.groups.add(Group.objects.get(name="Authors"))
        cls.person = Person.objects.create(name="作者李四", linked_user=cls.author)

        cls.blog_idx = BlogIndexPage(title="技术专栏", slug="md-tech-blog")
        cls.home.add_child(instance=cls.blog_idx)

        cls.guide_idx = GuideIndexPage(title="实战指南", slug="md-guides")
        cls.home.add_child(instance=cls.guide_idx)

        cls.proj_idx = PortfolioIndexPage(title="开源项目", slug="md-projects")
        cls.home.add_child(instance=cls.proj_idx)

    def _exchange_author_token(self) -> str:
        _, raw_code = issue_pair_code(user=self.author, site=self.site)
        c = Client()
        r1 = c.post(
            "/api/markdown-import/pair/exchange/",
            data=json.dumps({"pair_code": raw_code}),
            content_type="application/json",
        )
        self.assertEqual(r1.status_code, 200, r1.content)
        token = r1.json()["token"]

        # 配对码一次性作废
        r2 = c.post(
            "/api/markdown-import/pair/exchange/",
            data=json.dumps({"pair_code": raw_code}),
            content_type="application/json",
        )
        self.assertEqual(r2.status_code, 403)
        return token

    def test_catalog_and_parent_permission_isolation_and_csrf(self):
        token = self._exchange_author_token()
        c = Client()
        resp = c.get("/api/markdown-import/catalog/?type_key=blog_page", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        type_keys = {t["type_key"] for t in payload["types"]}
        self.assertEqual(type_keys, {"blog_page", "guide_page", "project_page"})
        parent_ids = {p["id"] for p in payload["parents"]}
        self.assertIn(self.blog_idx.id, parent_ids)
        # 受限作者绝不可直接将文章挂到 HomePage 下
        self.assertNotIn(self.home.id, parent_ids)

        # Session 未带 CSRF 的写请求必须被拒绝 403
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.author)
        bad_csrf = csrf_client.post(
            "/api/markdown-import/preflight/",
            data=json.dumps({"type_key": "blog_page", "parent_id": self.blog_idx.id, "markdown": "# hi"}),
            content_type="application/json",
        )
        self.assertEqual(bad_csrf.status_code, 403)

        # 吊销 Token 后即刻拒绝
        MarkdownImportToken.objects.filter(user=self.author).update(is_active=False)
        revoked_resp = c.get("/api/markdown-import/catalog/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(revoked_resp.status_code, 403)

    def test_blog_guide_project_full_import_dehydration_idempotency_and_reconciliation(self):
        token = self._exchange_author_token()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        c = Client()

        png_bytes = _make_png_bytes()
        zip_bytes = b"PK\x03\x04COURSEWARE_DEMO_ZIP_CONTENT"
        png_sha = hashlib.sha256(png_bytes).hexdigest()
        zip_sha = hashlib.sha256(zip_bytes).hexdigest()

        blog_md = (
            "---\n"
            "intro: 核心架构导入简介\n"
            "---\n\n"
            "# 跨存储脱水实战\n\n"
            "![架构图](images/arch.png)\n\n"
            "[下载课件](files/lab.zip)\n"
        )
        blog_edits = {"tags": "PostgreSQL, Wagtail, 新建标签Demo", "allow_new_tags": True}
        assets_manifest = [
            {"asset_key": "images/arch.png", "media_type": "image", "filename": "arch.png", "size": len(png_bytes), "sha256": png_sha},
            {"asset_key": "files/lab.zip", "media_type": "document", "filename": "lab.zip", "size": len(zip_bytes), "sha256": zip_sha},
        ]

        # 1. 预检
        pre = c.post(
            "/api/markdown-import/preflight/",
            data=json.dumps(
                {
                    "type_key": "blog_page",
                    "parent_id": self.blog_idx.id,
                    "source_name": "post.md",
                    "markdown": blog_md,
                    "assets": assets_manifest,
                    "edits": blog_edits,
                }
            ),
            content_type="application/json",
            **auth,
        )
        self.assertEqual(pre.status_code, 200, pre.content)
        pre_data = pre.json()
        self.assertEqual(pre_data["errors"], [])
        plan_token = pre_data["plan_token"]
        self.assertTrue(plan_token)

        # 2. 创建作业 + 校验 Idempotency-Key 冲突
        idem_key = f"idem-{uuid.uuid4().hex}"
        job_req = {
            "plan_token": plan_token,
            "type_key": "blog_page",
            "parent_id": self.blog_idx.id,
            "source_name": "post.md",
            "markdown": blog_md,
            "assets": assets_manifest,
            "edits": blog_edits,
        }
        j1 = c.post("/api/markdown-import/jobs/", data=json.dumps(job_req), content_type="application/json", HTTP_IDEMPOTENCY_KEY=idem_key, **auth)
        self.assertEqual(j1.status_code, 201, j1.content)
        job_id = j1.json()["job_id"]

        # 同键重放返回 200 原作业
        j_dup = c.post("/api/markdown-import/jobs/", data=json.dumps(job_req), content_type="application/json", HTTP_IDEMPOTENCY_KEY=idem_key, **auth)
        self.assertEqual(j_dup.status_code, 200)
        self.assertEqual(j_dup.json()["job_id"], job_id)

        # 3. 上传图片与课件
        u1 = c.post(
            f"/api/markdown-import/jobs/{job_id}/assets/",
            data={"asset_key": "images/arch.png", "file": SimpleUploadedFile("arch.png", png_bytes, content_type="image/png")},
            **auth,
        )
        self.assertEqual(u1.status_code, 200, u1.content)
        img_uuid = uuid.UUID(u1.json()["public_id"])

        u2 = c.post(
            f"/api/markdown-import/jobs/{job_id}/assets/",
            data={"asset_key": "files/lab.zip", "file": SimpleUploadedFile("lab.zip", zip_bytes, content_type="application/zip")},
            **auth,
        )
        self.assertEqual(u2.status_code, 200, u2.content)
        doc_uuid = uuid.UUID(u2.json()["public_id"])

        # 在未 commit 前跑 grace_period_days=0 的孤儿对账：活跃作业暂存资产必须被豁免！
        reconcile_dry = CrossStorageReconciler.reconcile_and_purge_orphan_media(dry_run=True, grace_period_days=0)
        candidate_ids = {item["asset_id"] for item in reconcile_dry["candidates"]}
        self.assertNotIn(str(img_uuid), candidate_ids)
        self.assertNotIn(str(doc_uuid), candidate_ids)

        # 4. 提交创建草稿（并验证重复提交整单幂等）
        cm1 = c.post(f"/api/markdown-import/jobs/{job_id}/commit/", **auth)
        self.assertEqual(cm1.status_code, 200, cm1.content)
        res1 = cm1.json()
        cm2 = c.post(f"/api/markdown-import/jobs/{job_id}/commit/", **auth)
        self.assertEqual(cm2.status_code, 200)
        self.assertEqual(cm1.json()["page_id"], cm2.json()["page_id"])

        page = BlogPage.objects.get(id=res1["page_id"])
        self.assertFalse(page.live)
        self.assertEqual(page.owner_id, self.author.id)
        self.assertEqual(page.authors.first().person_id, self.person.id)
        self.assertEqual(
            set(page.tags.values_list("name", flat=True)),
            {"PostgreSQL", "Wagtail", "新建标签Demo"},
        )
        # 主表与 Revision 内容均严格脱水为 []
        self.assertEqual(list(page.intro), [])
        self.assertEqual(list(page.body), [])
        rev = page.latest_revision
        self.assertEqual(rev.content["intro"], [])
        self.assertEqual(rev.content["body"], [])

        # 通过 with_content_json 从 Mongo 水合验证正文已剥离重复 H1 且重写为 asset://
        hydrated = rev.as_object()
        self.assertEqual(hydrated.intro[0].value, "核心架构导入简介")
        body_md = hydrated.body[0].value
        self.assertNotIn("# 跨存储脱水实战", body_md)
        self.assertIn(f"asset://image/{img_uuid}", body_md)
        self.assertIn(f"asset://document/{doc_uuid}", body_md)

        # 验证 MEDIA-REF-01 媒体引用登记完整且 OperationResult 落账
        ref_ids = set(
            str(aid)
            for aid in MediaReferenceRecord.objects.filter(page_id=page.id, revision_id=rev.id).values_list("asset_id", flat=True)
        )
        self.assertIn(str(img_uuid), ref_ids)
        self.assertIn(str(doc_uuid), ref_ids)
        self.assertTrue(OperationResult.objects.filter(operation_id=f"markdown_import:{job_id}").exists())

        # 验证“受限页面”发布时绝不自动把受限附件提升为 PUBLIC
        PageViewRestriction.objects.create(page=page, restriction_type=PageViewRestriction.LOGIN)
        page.save_revision(user=self.author).publish(user=self.author)
        self.assertEqual(CustomDocument.objects.get(public_id=doc_uuid).visibility, VisibilityChoices.RESTRICTED)

        # 5. 测试 GuidePage 缺 topic/step 可建草稿但不可绕过发布门禁
        g_pre = c.post(
            "/api/markdown-import/preflight/",
            data=json.dumps(
                {
                    "type_key": "guide_page",
                    "parent_id": self.guide_idx.id,
                    "source_name": "g.md",
                    "markdown": "# 无步骤指南草稿\n\n纯文字正文。",
                    "assets": [],
                }
            ),
            content_type="application/json",
            **auth,
        ).json()
        self.assertEqual(len(g_pre["publish_blockers"]), 2)
        g_job = c.post(
            "/api/markdown-import/jobs/",
            data=json.dumps(
                {
                    "plan_token": g_pre["plan_token"],
                    "type_key": "guide_page",
                    "parent_id": self.guide_idx.id,
                    "source_name": "g.md",
                    "markdown": "# 无步骤指南草稿\n\n纯文字正文。",
                    "assets": [],
                }
            ),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=f"g-{uuid.uuid4().hex}",
            **auth,
        ).json()
        g_cm = c.post(f"/api/markdown-import/jobs/{g_job['job_id']}/commit/", **auth)
        self.assertEqual(g_cm.status_code, 200)
        g_page = GuidePage.objects.get(id=g_cm.json()["page_id"])
        with self.assertRaises(ValidationError):
            g_page.validate_for_publish(user=self.author)

        # 6. 测试 ProjectPage 含仓库地址与脱水 body
        p_md = "---\nproject_status: in_development\nrepo_url: https://github.com/anc1996/myblog\n---\n\n# 开源作品集测试\n\n详细内容。\n"
        p_pre = c.post(
            "/api/markdown-import/preflight/",
            data=json.dumps(
                {
                    "type_key": "project_page",
                    "parent_id": self.proj_idx.id,
                    "source_name": "p.md",
                    "markdown": p_md,
                    "assets": [],
                }
            ),
            content_type="application/json",
            **auth,
        ).json()
        p_job = c.post(
            "/api/markdown-import/jobs/",
            data=json.dumps(
                {
                    "plan_token": p_pre["plan_token"],
                    "type_key": "project_page",
                    "parent_id": self.proj_idx.id,
                    "source_name": "p.md",
                    "markdown": p_md,
                    "assets": [],
                }
            ),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=f"p-{uuid.uuid4().hex}",
            **auth,
        ).json()
        p_cm = c.post(f"/api/markdown-import/jobs/{p_job['job_id']}/commit/", **auth)
        self.assertEqual(p_cm.status_code, 200)
        p_page = ProjectPage.objects.get(id=p_cm.json()["page_id"])
        self.assertEqual(p_page.project_status, "in_development")
        self.assertEqual(p_page.repo_url, "https://github.com/anc1996/myblog")
        self.assertEqual(list(p_page.body), [])
        self.assertEqual(p_page.authors.first().person_id, self.person.id)

    def test_cli_template_and_client_checkpoint_without_token_leak(self):
        self.assertEqual(cli_main(["template", "guide_page"]), 0)
        token = self._exchange_author_token()
        django_client = Client()

        def _bridge_request(method, url, headers=None, json_body=None, data=None, files=None, params=None):
            path = "/" + url.split("://", 1)[1].split("/", 1)[1]
            extra = {}
            if headers:
                for k, v in headers.items():
                    extra[f"HTTP_{k.upper().replace('-', '_')}"] = v
            if method == "GET":
                r = django_client.get(path, data=params or {}, **extra)
            elif json_body is not None:
                r = django_client.post(path, data=json.dumps(json_body), content_type="application/json", **extra)
            else:
                payload = dict(data or {})
                if files:
                    for fk, (fname, fh) in files.items():
                        payload[fk] = SimpleUploadedFile(fname, fh.read())
                r = django_client.post(path, data=payload, **extra)
            resp = mock.MagicMock()
            resp.status_code = r.status_code
            resp.reason = ""
            resp.text = r.content.decode("utf-8", errors="ignore")
            resp.json = r.json
            return resp

        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "note.md"
            md_path.write_text("# 客户端联调测试\n\n正文内容。", encoding="utf-8")
            scanned = scan_markdown_source(md_path)
            with self.assertRaises(ValueError):
                MarkdownImportClient("http://example.com", token=token)
            client = MarkdownImportClient("https://testserver", token=token)
            with mock.patch("tools.markdown_import.client.requests.post", side_effect=lambda u, **kw: _bridge_request("POST", u, headers=kw.get("headers"), json_body=kw.get("json"), data=kw.get("data"), files=kw.get("files"))):
                res = client.import_source(type_key="blog_page", parent_id=self.blog_idx.id, scanned=scanned)
            self.assertEqual(res["status"], "draft_created")
            cp = load_session_checkpoint(md_path)
            self.assertIsNotNone(cp)
            self.assertEqual(cp["page_id"], res["page_id"])
            raw_cp_text = md_path.with_name(".note.md.markdown-import-session.json").read_text(encoding="utf-8")
            self.assertNotIn(token, raw_cp_text)

    def test_security_hardening_session_epoch_manifest_and_mongo_retry_stability(self):
        from identity.account_lifecycle import AccountLifecycleService

        # 1. session_epoch 递增后，旧导入令牌与未兑换配对码同时失效
        _, stale_code = issue_pair_code(user=self.author, site=self.site)
        token = self._exchange_author_token()
        AccountLifecycleService.bump_session_epoch([self.author.id])

        c = Client()
        self.assertEqual(
            c.get("/api/markdown-import/catalog/", HTTP_AUTHORIZATION=f"Bearer {token}").status_code,
            403,
        )
        self.assertEqual(
            c.post(
                "/api/markdown-import/pair/exchange/",
                data=json.dumps({"pair_code": stale_code}),
                content_type="application/json",
            ).status_code,
            403,
        )

        # 2. 0 字节、负大小或空哈希清单被拒绝 422
        fresh_token = self._exchange_author_token()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {fresh_token}"}
        for bad_asset in (
            {"asset_key": "a.png", "media_type": "image", "size": 0, "sha256": "a" * 64},
            {"asset_key": "a.png", "media_type": "image", "size": -5, "sha256": "a" * 64},
            {"asset_key": "a.png", "media_type": "image", "size": 10, "sha256": ""},
        ):
            bad_pre = c.post(
                "/api/markdown-import/preflight/",
                data=json.dumps(
                    {
                        "type_key": "blog_page",
                        "parent_id": self.blog_idx.id,
                        "source_name": "bad.md",
                        "markdown": "# test",
                        "assets": [bad_asset],
                    }
                ),
                content_type="application/json",
                **auth,
            )
            self.assertEqual(bad_pre.status_code, 422)

        # 3. Mongo 写入后注入 SQL 事务回滚，再次提交同一作业必须成功且不触发 Mongo 哈希冲突
        ok_pre = c.post(
            "/api/markdown-import/preflight/",
            data=json.dumps(
                {
                    "type_key": "blog_page",
                    "parent_id": self.blog_idx.id,
                    "source_name": "retry.md",
                    "markdown": "# 重试一致性测试\n\n这是确定性积木 ID 测试正文。",
                    "assets": [],
                }
            ),
            content_type="application/json",
            **auth,
        ).json()
        job_resp = c.post(
            "/api/markdown-import/jobs/",
            data=json.dumps(
                {
                    "plan_token": ok_pre["plan_token"],
                    "type_key": "blog_page",
                    "parent_id": self.blog_idx.id,
                    "source_name": "retry.md",
                    "markdown": "# 重试一致性测试\n\n这是确定性积木 ID 测试正文。",
                    "assets": [],
                }
            ),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=f"retry-{uuid.uuid4().hex}",
            **auth,
        ).json()
        job_id = job_resp["job_id"]

        with mock.patch("markdown_import.services.OperationResult.objects.get_or_create", side_effect=RuntimeError("simulated sql failure")):
            with self.assertRaises(RuntimeError):
                c.post(f"/api/markdown-import/jobs/{job_id}/commit/", **auth)

        # 确认 SQL 回滚后没有创建出游离页面，然后第二次正常提交必须成功
        self.assertIsNone(ImportJob.objects.get(job_id=job_id).result_page_id)
        retry_commit = c.post(f"/api/markdown-import/jobs/{job_id}/commit/", **auth)
        self.assertEqual(retry_commit.status_code, 200, retry_commit.content)
        self.assertEqual(retry_commit.json()["status"], "draft_created")
    def test_catalog_parent_filtering_search_and_local_tag_suggestion(self):
        from taggit.models import Tag
        Tag.objects.get_or_create(name="PostgreSQL")
        Tag.objects.get_or_create(name="Wagtail")

        # 给超级管理员建一个 BlogPage（BlogPage.subpage_types 允许挂子 BlogPage）
        admin_user = User.objects.create_superuser(username="md_admin", password="PassWord12345!", email="a@b.c")
        sub_post = BlogPage(title="可以挂子文章的母文章", slug="parent-blog-post")
        self.blog_idx.add_child(instance=sub_post)

        _, raw_code = issue_pair_code(user=admin_user, site=self.site)
        c = Client()
        token = c.post(
            "/api/markdown-import/pair/exchange/",
            data=json.dumps({"pair_code": raw_code}),
            content_type="application/json",
        ).json()["token"]
        auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

        # 1. 默认 containers_only=1 隐藏文章页，只显示栏目/首页，并携带 breadcrumb_path 与 allowed_child_types
        r_containers = c.get("/api/markdown-import/catalog/?type_key=blog_page&containers_only=1", **auth).json()
        ids_only = {p["id"] for p in r_containers["parents"]}
        self.assertIn(self.blog_idx.id, ids_only)
        self.assertNotIn(sub_post.id, ids_only)
        idx_entry = next(p for p in r_containers["parents"] if p["id"] == self.blog_idx.id)
        self.assertIn("技术专栏", idx_entry["breadcrumb_path"])
        self.assertEqual([t["type_key"] for t in idx_entry["allowed_child_types"]], ["blog_page"])

        # 2. 取消 containers_only 并用 q 搜索母文章时可精确命中且排在容器之后
        r_all = c.get("/api/markdown-import/catalog/?type_key=blog_page&containers_only=0&q=母文章", **auth).json()
        self.assertEqual([p["id"] for p in r_all["parents"]], [sub_post.id])
        self.assertFalse(r_all["parents"][0]["is_container"])

        # 3. 无外部 AI 时对含大量通用管理词的文章也能召回站内技术标签 + 新标签
        ai_res = c.post(
            "/api/markdown-import/ai/suggest/",
            data=json.dumps(
                {
                    "type_key": "blog_page",
                    "parent_id": self.blog_idx.id,
                    "source_name": "AGENTS.md",
                    "markdown": "项目规则必须检查测试与提交。\n使用 PostgreSQL 与 Wagtail 架构。\n",
                    "allow_external_processing": True,
                    "allow_new_tags": True,
                }
            ),
            content_type="application/json",
            **auth,
        )
        self.assertEqual(ai_res.status_code, 200)
        sug_tags = ai_res.json()["suggestions"]["tags"]
        self.assertIn("PostgreSQL", sug_tags)
        self.assertIn("Wagtail", sug_tags)
