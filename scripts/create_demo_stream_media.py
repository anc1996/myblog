import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
import django
django.setup()

from django.contrib.auth import get_user_model
from wagtail.rich_text import RichText
from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from people.models import Person

User = get_user_model()
admin_user = User.objects.filter(is_superuser=True).first()
author = Person.objects.first()

index_page = BlogIndexPage.objects.filter(slug="qa-test-blog").first()
if not index_page:
    index_page = BlogIndexPage.objects.first()

slug = "tc-blk-11-stream-media"
existing = BlogPage.objects.filter(slug=slug).first()
if not existing:
    page = BlogPage(
        title="流媒体与第三方内容嵌入效果演示",
        slug=slug,
        owner=admin_user,
        authored_on=date(2026, 11, 20),
    )
    index_page.add_child(instance=page)
else:
    page = existing

page.intro = [
    ("rich_text", RichText("<p>本篇展示全站统一流媒体与嵌入框架（<code>content_blocks.embeds</code>）对各大主流音视频与代码平台的受控接入效果。所有嵌入元素均自带<strong>「媒体标题 / 无障碍描述」</strong>，强制沙箱隔离与白名单校验，彻底防御 SSRF 攻击。</p>"))
]

page.body = [
    ("markdown", """## 一、 视频平台流媒体嵌入 (16:9 响应式沙箱播放器)
包含哔哩哔哩 (Bilibili)、YouTube、Vimeo、腾讯视频、优酷视频。支持全屏、自适应容器，顶部自动提取媒体真实标题与源链接导航。"""),
    ("embed", {
        "title": "哔哩哔哩：计算机网络微课堂精选教程",
        "embed_url": "https://www.bilibili.com/video/BV1xx411c7mD",
    }),
    ("embed", {
        "title": "YouTube：Me at the zoo (YouTube 史上首支公开视频，支持无限制内嵌播放)",
        "embed_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    }),
    ("embed", {
        "title": "Vimeo 官方播放器演示",
        "embed_url": "https://vimeo.com/76979871/8272103f6e",
    }),
    ("embed", {
        "title": "地球奇观：如果其他星球代替了月亮",
        "embed_url": "https://v.qq.com/x/page/i0145yslf2c.html",
    }),
    ("embed", {
        "title": "优酷视频：开源数据库性能测试纪录片",
        "embed_url": "https://v.youku.com/v_show/id_XMTIzNDU2.html",
    }),
    ("markdown", """## 二、 音乐与音频流媒体嵌入 (紧凑播放器 / 安全外链卡片)
网易云音乐与 Spotify 支持紧凑/音频卡片播放器；酷狗音乐和咪咕音乐显示外链卡片。"""),
    ("embed", {
        "title": "Spotify：Billie Eilish - bad guy (官方音乐播放器)",
        "embed_url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
    }),
    ("embed", {
        "title": "网易云音乐：夜的钢琴曲五（轻音乐精选）",
        "embed_url": "https://music.163.com/#/song?id=186016",
    }),
    ("embed", {
        "title": "酷狗音乐：精选华语流行热歌",
        "embed_url": "https://www.kugou.com/mixsong/abc123.html",
    }),
    ("embed", {
        "title": "咪咕音乐：高品质正版无损音乐原声",
        "embed_url": "https://music.migu.cn/v3/music/song/60054701933",
    }),
    ("markdown", """## 三、 代码与交互演示平台嵌入
支持 CodePen 等前端可交互代码与效果实时预览。"""),
    ("embed", {
        "title": "CodePen：CSS3 3D 动画与滤镜效果交互演练",
        "embed_url": "https://codepen.io/team/codepen/pen/PNaGbb",
    }),
    ("markdown", """## 四、 Draftail 富文本内嵌同构演示
富文本编辑器中通过 `▶` 插入的内嵌元素，与 StreamField 独立嵌入积木共用完全相同的渲染底座："""),
    ("rich_text", RichText('<p>下面是富文本内插入的带标题流媒体：</p><embed embedtype="media" url="https://www.bilibili.com/video/BV1xx411c7mD" title="富文本段落内嵌：Bilibili 架构实战精选"/>')),
]

page.save()
if author:
    BlogPagePerson.objects.create(page=page, person=author, role="主要作者", sort_order=0)
rev = page.save_revision(user=admin_user)
rev.publish(user=admin_user)

print("Created page successfully!")
print(f"ID={page.id}, URL={page.url}")
