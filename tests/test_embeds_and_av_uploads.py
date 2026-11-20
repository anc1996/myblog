"""
Step 1 & Step 2 集成与权限验收测试：
1. 统一内嵌媒体框架：EmbedProviderRegistry、RegistryEmbedFinder、Draftail 自定义标题弹窗与 Contentstate 往返；
2. 音视频弹窗上传与作者隔离：Audio/Video SnippetChooser 双标签页上传、格式/体积校验、作者隔离选用/修改与发布自动转公开。
"""

from datetime import date
import json
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from wagtail.models import Page
from wagtail.rich_text import RichText
from wagtail.test.utils.form_data import nested_form_data, rich_text

from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from content_blocks.blocks.rich_text import CustomRichTextBlock
from content_blocks.embeds.finder import RegistryEmbedFinder
from content_blocks.embeds.rich_text import ControlledContentstateMediaConversionRule
from content_lifecycle.permissions import ensure_author_role_infrastructure
from media_library.models import Audio, Video, VisibilityChoices
from people.models import Person

User = get_user_model()


class UnifiedEmbedAndAVUploadTests(TestCase):

    def setUp(self):
        self.infra = ensure_author_role_infrastructure()
        self.superuser = User.objects.create_superuser(
            username=f"su_embed_{uuid4().hex[:6]}",
            email="su_embed@example.com",
            password="Password123!",
        )
        self.author_a = User.objects.create_user(
            username=f"author_a_{uuid4().hex[:6]}",
            password="Password123!",
        )
        self.author_b = User.objects.create_user(
            username=f"author_b_{uuid4().hex[:6]}",
            password="Password123!",
        )
        self.author_a.groups.add(self.infra["authors_group"])
        self.author_b.groups.add(self.infra["authors_group"])

        self.person_a = Person.objects.create(name=f"作者甲-{uuid4().hex[:6]}", linked_user=self.author_a)
        root_target = self.infra["target_root_page"]
        self.blog_index = BlogIndexPage(title="Embed测试专栏", slug=f"eb-{uuid4().hex[:6]}")
        root_target.add_child(instance=self.blog_index)
        self.blog_index.save_revision().publish()

    def test_draftail_embed_chooser_title_and_ssrf_validation(self):
        """验证 Draftail 插入内嵌元素弹窗包含媒体标题字段、支持新平台并拦截 SSRF"""
        self.client.force_login(self.author_a)
        chooser_url = reverse("wagtailembeds:chooser")
        upload_url = reverse("wagtailembeds:chooser_upload")

        resp_get = self.client.get(chooser_url)
        self.assertEqual(resp_get.status_code, 200)
        modal_html = resp_get.json()["html"]
        self.assertIn("媒体标题 / 无障碍描述", modal_html)
        self.assertIn("网易云音乐", modal_html)

        # 1. 提交合法网易云音乐链接 + 自定义媒体标题
        resp_ok = self.client.post(
            upload_url,
            {
                "embed-chooser-title": "夜的钢琴曲测试",
                "embed-chooser-url": "https://music.163.com/#/song?id=186016",
            },
        )
        self.assertEqual(resp_ok.status_code, 200)
        payload = resp_ok.json()
        self.assertEqual(payload["step"], "embed_chosen")
        self.assertEqual(payload["embed_data"]["title"], "夜的钢琴曲测试")
        self.assertEqual(payload["embed_data"]["providerName"], "网易云音乐")

        # 2. 提交内网 SSRF 地址必须被拒绝
        resp_ssrf = self.client.post(
            upload_url,
            {
                "embed-chooser-title": "非法内网",
                "embed-chooser-url": "http://127.0.0.1/secret",
            },
        )
        self.assertEqual(resp_ssrf.status_code, 200)
        self.assertEqual(resp_ssrf.json()["step"], "chooser")
        self.assertIn("SSRF", resp_ssrf.json()["html"])

    def test_draftail_contentstate_preserves_custom_title_and_renders_same_wrapper(self):
        """验证 Draftail Contentstate 双向转换保留自定义标题，且 RichText 与 EmbedBlock 输出一致容器"""
        from wagtail.admin.rich_text.converters.contentstate import ContentstateConverter

        converter = ContentstateConverter(
            features=["bold", "embed"]
        )
        db_html = '<p>引言</p><embed embedtype="media" url="https://www.bilibili.com/video/BV1xx411c7mD" title="PostgreSQL 18 课程"/>'
        cs_json = converter.from_database_format(db_html)
        cs_data = json.loads(cs_json)
        entity_vals = list(cs_data["entityMap"].values())
        self.assertEqual(len(entity_vals), 1)
        self.assertEqual(entity_vals[0]["data"]["title"], "PostgreSQL 18 课程")

        restored_db = converter.to_database_format(cs_json)
        self.assertIn('title="PostgreSQL 18 课程"', restored_db)

        rt_block = CustomRichTextBlock()
        rendered = rt_block.render_clean(RichText(restored_db))
        self.assertIn('data-embed-title="PostgreSQL 18 课程"', rendered.html)
        self.assertIn('data-embed-provider="bilibili"', rendered.html)
        self.assertIn('data-embed-src="https://player.bilibili.com/player.html?bvid=BV1xx411c7mD', rendered.html)

        finder = RegistryEmbedFinder()
        url = "https://music.migu.cn/v3/music/song/60054701933"
        self.assertTrue(finder.accept(url))
        found = finder.find_embed(url, title="咪咕测试")
        self.assertEqual(found["title"], "咪咕测试")
        self.assertIn('data-embed-kind="audio"', found["html"])
        self.assertNotIn("data-embed-src=", found["html"])

    def test_audio_and_video_chooser_upload_and_author_isolation(self):
        """验证音视频页面选择器内支持直接上传、扩展名校验、作者隔离与发布自动转公开"""
        audio_choose_url = reverse("wagtailsnippetchoosers_media_library_audio:choose")
        audio_create_url = reverse("wagtailsnippetchoosers_media_library_audio:create")
        video_choose_url = reverse("wagtailsnippetchoosers_media_library_video:choose")
        video_create_url = reverse("wagtailsnippetchoosers_media_library_video:create")

        self.client.force_login(self.author_a)

        # 1. 选择器首页应包含“上传”表单按钮
        resp_choose_a = self.client.get(audio_choose_url)
        self.assertEqual(resp_choose_a.status_code, 200)
        self.assertIn("上传并选用", resp_choose_a.json()["html"])

        # 2. 非法扩展名必须拦截
        bad_audio = SimpleUploadedFile("evil.html", b"<script>1</script>", content_type="text/html")
        resp_bad = self.client.post(
            audio_create_url,
            {"title": "非法音频", "file": bad_audio},
        )
        self.assertEqual(resp_bad.status_code, 200)
        self.assertEqual(resp_bad.json()["step"], "reshow_creation_form")
        bad_payload = resp_bad.json()
        self.assertIn("不支持的音频格式", bad_payload.get("htmlFragment") or bad_payload.get("html", ""))
        self.assertEqual(Audio.objects.filter(title="非法音频").count(), 0)

        # 3. 作者 A 成功上传受限音频与视频
        good_audio = SimpleUploadedFile("demo.mp3", b"ID3-fake-mp3", content_type="audio/mpeg")
        resp_aud_ok = self.client.post(
            audio_create_url,
            {"title": "作者A私有音频", "file": good_audio},
        )
        self.assertEqual(resp_aud_ok.status_code, 200)
        self.assertEqual(resp_aud_ok.json()["step"], "chosen")

        good_video = SimpleUploadedFile("clip.mp4", b"ftypmp42-fake", content_type="video/mp4")
        resp_vid_ok = self.client.post(
            video_create_url,
            {"title": "作者A私有视频", "file": good_video},
        )
        self.assertEqual(resp_vid_ok.status_code, 200)
        self.assertEqual(resp_vid_ok.json()["step"], "chosen")

        aud_a = Audio.objects.get(title="作者A私有音频")
        vid_a = Video.objects.get(title="作者A私有视频")
        self.assertEqual(aud_a.uploaded_by_user_id, self.author_a.id)
        self.assertEqual(aud_a.visibility, VisibilityChoices.RESTRICTED)
        self.assertTrue(bool(aud_a.storage_key))
        self.assertEqual(vid_a.uploaded_by_user_id, self.author_a.id)
        self.assertEqual(vid_a.visibility, VisibilityChoices.RESTRICTED)

        # 4. 作者 B 打开音视频选择器时绝不能看到作者 A 的受限资产
        self.client.force_login(self.author_b)
        resp_choose_b_aud = self.client.get(audio_choose_url)
        self.assertNotIn("作者A私有音频", resp_choose_b_aud.json()["html"])
        resp_choose_b_vid = self.client.get(video_choose_url)
        self.assertNotIn("作者A私有视频", resp_choose_b_vid.json()["html"])

        # 5. 作者 A 在本人文章中引用自己上传的音视频并发布 -> 放行且自动转为 PUBLIC
        blog = BlogPage(
            title="音视频自传发布测试",
            slug=f"av-own-{uuid4().hex[:6]}",
            owner=self.author_a,
            authored_on=date(2026, 11, 20),
        )
        blog.body = [
            ("audio", {"audio": aud_a, "title_override": "播客试听"}),
            ("video", {"video": vid_a, "title_override": "实战录像"}),
        ]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        rev = blog.save_revision(user=self.author_a)
        rev.publish(user=self.author_a)

        aud_a.refresh_from_db()
        vid_a.refresh_from_db()
        self.assertEqual(aud_a.visibility, VisibilityChoices.PUBLIC)
        self.assertEqual(vid_a.visibility, VisibilityChoices.PUBLIC)

        # 6. 一旦转公开，作者 B 现在可以在选择器中选用该公共音视频
        resp_choose_b_pub = self.client.get(video_choose_url)
        self.assertIn("作者A私有视频", resp_choose_b_pub.json()["html"])

        # 7. 媒体保护：已被页面引用的音频禁止通过表单覆盖替换原文件
        from media_library.forms import AudioUploadForm
        replace_file = SimpleUploadedFile("new.mp3", b"new-bytes", content_type="audio/mpeg")
        form_replace = AudioUploadForm(
            data={"title": "作者A私有音频"},
            files={"file": replace_file},
            instance=aud_a,
        )
        self.assertFalse(form_replace.is_valid())
        self.assertIn("禁止直接覆盖替换原文件", str(form_replace.errors))
