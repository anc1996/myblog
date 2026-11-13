"""
Phase 5: 开发库 (.env.local) 多角色实机运行冒烟探针。
验证：超管登录、创作者登录、栏目建页跳转、人物名片预填、发布自动公开配图、
跨作者编辑隔离、回收站只读与超管物理销毁。
"""

import os
import sys
from pathlib import Path
from io import BytesIO
from uuid import uuid4

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from myblog.bootstrap import configure_app_imports
configure_app_imports()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from PIL import Image as PILImage
from wagtail.models import Page

from blog.models import BlogIndexPage, BlogPage
from content_lifecycle.permissions import ensure_author_role_infrastructure
from content_lifecycle.recycle import RecycleService
from home.models import HomePage
from identity.models import User
from media_library.models import CustomImage, VisibilityChoices
from people.models import Person


def main():
    print("=== 开始 Phase 5 多角色实机开发库冒烟探针 ===")
    infra = ensure_author_role_infrastructure()
    authors_group = infra["authors_group"]
    workspace_col = infra["workspace_collection"]

    su, _ = User.objects.get_or_create(username="admin", defaults={"is_superuser": True, "is_staff": True})
    su.set_password("admin123456")
    su.save()

    suffix = uuid4().hex[:6]
    auth_a = User.objects.create_user(username=f"dev_auth_a_{suffix}", password="Password123!", is_staff=False)
    auth_a.groups.add(authors_group)
    person_a = Person.objects.create(name=f"Dev Person A {suffix}", linked_user=auth_a)

    auth_b = User.objects.create_user(username=f"dev_auth_b_{suffix}", password="Password123!", is_staff=False)
    auth_b.groups.add(authors_group)

    # 1. 管理员登录
    c_admin = Client()
    logged_in = c_admin.login(username="admin", password="admin123456")
    assert logged_in, "Admin login failed"
    res = c_admin.get("/admin/")
    assert res.status_code == 200, f"Admin index status: {res.status_code}"
    print("✓ 1. 超级管理员后台登录正常 (200 OK)")

    # 2. 创作者登录 (is_staff=False, access_admin=True)
    c_a = Client()
    logged_in = c_a.login(username=auth_a.username, password="Password123!")
    assert logged_in, "Author A login failed"
    res_a = c_a.get("/admin/")
    assert res_a.status_code == 200, f"Author A admin status: {res_a.status_code}"
    print("✓ 2. 创作者 Author A (is_staff=False, 仅授 access_admin) 登录管理后台正常 (200 OK)")

    # 3. 栏目建页与自动预填人物
    home = HomePage.objects.first()
    if not home:
        home = HomePage(title="Home", slug="home")
        Page.get_first_root_node().add_child(instance=home)
    blog_index = BlogIndexPage.objects.first()
    if not blog_index:
        blog_index = BlogIndexPage(title="博客大厅", slug="blog")
        home.add_child(instance=blog_index)
        blog_index.save_revision().publish()

    res_sub = c_a.get(reverse("wagtailadmin_pages:add_subpage", args=[blog_index.id]))
    assert res_sub.status_code == 302 and "blog/blogpage" in res_sub["Location"]
    print("✓ 3. 创作者在栏目下点击新建子页自动直达 BlogPage 表单 (302 Redirect)")

    res_form = c_a.get(reverse("wagtailadmin_pages:add", args=["blog", "blogpage", blog_index.id]))
    assert res_form.status_code == 200
    expected_field = f'name="authors-0-person" value="{person_a.id}"'
    assert expected_field in res_form.content.decode("utf-8")
    print("✓ 4. 创作者打开新建页面自动预填本人关联的 Person 署名 (200 OK)")

    # 4. 发布带受限图片的页面并自动公开
    buf = BytesIO()
    PILImage.new("RGB", (32, 32), color=(200, 100, 50)).save(buf, format="PNG")
    img_file = SimpleUploadedFile(f"dev_{suffix}.png", buf.getvalue(), content_type="image/png")
    img = CustomImage.objects.create(
        title=f"Dev Image {suffix}",
        file=img_file,
        collection=workspace_col,
        uploaded_by_user=auth_a,
        visibility=VisibilityChoices.RESTRICTED,
    )
    page_a = BlogPage(title=f"Dev Article {suffix}", slug=f"dev-art-{suffix}", owner=auth_a, featured_image=img)
    blog_index.add_child(instance=page_a)
    rev = page_a.save_revision(user=auth_a)
    rev.publish(user=auth_a)
    img.refresh_from_db()
    assert img.visibility == VisibilityChoices.PUBLIC, f"Image not promoted: {img.visibility}"
    c_anon = Client()
    res_img = c_anon.get(f"/media-gateway/image/{img.public_id}/")
    assert res_img.status_code == 200, f"Public gateway media status: {res_img.status_code}"
    print("✓ 5. 页面正式发布时配图自动由 RESTRICTED 提升为 PUBLIC，匿名通过网关访问 200 OK")

    # 5. 跨作者越权编辑拦截
    c_b = Client()
    c_b.login(username=auth_b.username, password="Password123!")
    res_edit_b = c_b.get(reverse("wagtailadmin_pages:edit", args=[page_a.id]))
    assert res_edit_b.status_code == 302 and res_edit_b["Location"] == "/admin/"
    print("✓ 6. 创作者 Author B 尝试访问 Author A 的页面编辑页被安全拦截重定向 (302 -> /admin/)")

    # 6. 回收站隔离与超管彻底销毁
    batch = RecycleService.recycle_page_or_subtree(page_a, actor=auth_a)
    res_bin_a = c_a.get("/admin/recycle_bin/")
    assert page_a.title in res_bin_a.content.decode("utf-8")
    assert "彻底销毁" not in res_bin_a.content.decode("utf-8")
    print("✓ 7. Author A 打开回收站列表仅见自己批次，未渲染'彻底销毁'危险按钮 (200 OK)")

    res_bin_b = c_b.get("/admin/recycle_bin/")
    assert page_a.title not in res_bin_b.content.decode("utf-8")
    print("✓ 8. Author B 打开回收站无法看到 Author A 的回收批次 (归属过滤正常)")

    res_bin_su = c_admin.get("/admin/recycle_bin/")
    assert "彻底销毁" in res_bin_su.content.decode("utf-8")
    res_purge = c_admin.post(f"/admin/recycle_bin/{batch.pk}/purge/", follow=True)
    assert res_purge.status_code == 200
    batch.refresh_from_db()
    assert batch.status == "purged"
    print("✓ 9. 超级管理员成功执行彻底销毁 Purge (200 OK, 批次状态转为 purged)")

    print("=== 全套 Phase 5 开发库实机冒烟校验全部通过 (9/9 OK) ===")


if __name__ == "__main__":
    main()
