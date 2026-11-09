"""
全站统一分类与标签词库 (Taxonomy)。

落实核心架构原则：
1. 全站共用单一 Tag 标签词库，不建独立 Category 模型；
2. TagMergeRedirect: 记录标签合并映射与重定向关系，确保合并后旧关联与搜索投影不失效。
"""

import hashlib
from django.db import models
from django.http import Http404, JsonResponse
from django.template.loader import render_to_string
from taggit.models import Tag
from wagtail.models import Page
from wagtail.admin.panels import FieldPanel
from wagtail.contrib.routable_page.models import RoutablePageMixin, path
from content_lifecycle.models import CustomBasePage


def get_pinyin_initial(text: str) -> str:
    """提取字符的首字母，英文字母返回大写，中文字符按标准GBK拼音映射，其他符号返回 # (BLK-04)"""
    if not text:
        return "#"
    c = text.strip()[0]
    if "a" <= c <= "z" or "A" <= c <= "Z":
        return c.upper()
    try:
        code = c.encode("gbk")
        if len(code) == 2:
            val = code[0] * 256 + code[1]
            if 0xB0A1 <= val <= 0xB0C4: return "A"
            if 0xB0C5 <= val <= 0xB2C0: return "B"
            if 0xB2C1 <= val <= 0xB4ED: return "C"
            if 0xB4EE <= val <= 0xB6E9: return "D"
            if 0xB6EA <= val <= 0xB7A1: return "E"
            if 0xB7A2 <= val <= 0xB8C0: return "F"
            if 0xB8C1 <= val <= 0xB9FD: return "G"
            if 0xB9FE <= val <= 0xBBF6: return "H"
            if 0xBBF7 <= val <= 0xBFA5: return "J"
            if 0xBFA6 <= val <= 0xC0AB: return "K"
            if 0xC0AC <= val <= 0xC2E7: return "L"
            if 0xC2E8 <= val <= 0xC4C2: return "M"
            if 0xC4C3 <= val <= 0xC5B5: return "N"
            if 0xC5B6 <= val <= 0xC5BD: return "O"
            if 0xC5BE <= val <= 0xC6D9: return "P"
            if 0xC6DA <= val <= 0xC8BA: return "Q"
            if 0xC8BB <= val <= 0xC8F5: return "R"
            if 0xC8F6 <= val <= 0xCBF9: return "S"
            if 0xCBFA <= val <= 0xCDD9: return "T"
            if 0xCDDA <= val <= 0xCEF3: return "W"
            if 0xCEF4 <= val <= 0xD1B8: return "X"
            if 0xD1B9 <= val <= 0xD4D0: return "Y"
            if 0xD4D1 <= val <= 0xD7F9: return "Z"
    except Exception:
        pass
    return "#"


class TagMergeRedirect(models.Model):
    """标签合并映射表：记录被合并的旧标签到新规范标签的重定向"""
    source_tag_id = models.IntegerField(db_index=True, verbose_name="原标签ID")
    source_name = models.CharField(max_length=100, db_index=True, verbose_name="原标签名称")
    target_tag = models.ForeignKey(
        Tag,
        on_delete=models.CASCADE,
        related_name="merged_from_records",
        verbose_name="合并后目标标签",
    )
    merged_at = models.DateTimeField(auto_now_add=True, verbose_name="合并时间")
    merged_by = models.CharField(max_length=150, blank=True, verbose_name="操作人")

    class Meta:
        verbose_name = "标签合并重定向"
        verbose_name_plural = "标签合并重定向"
        ordering = ["-merged_at"]

    def __str__(self):
        return f"Tag '{self.source_name}' -> '{self.target_tag.name}'"


class TagDirectoryMetadata(models.Model):
    """标签大厅目录元数据表 (首字母分组、排序键与官方简介)"""
    tag = models.OneToOneField(
        Tag,
        on_delete=models.CASCADE,
        related_name="directory_meta",
        verbose_name="关联标签",
    )
    initial = models.CharField(max_length=1, default="#", db_index=True, verbose_name="首字母分组")
    initial_group_rank = models.SmallIntegerField(default=0, db_index=True, verbose_name="分组桶序(0=A-Z,1=#)")
    sort_key = models.CharField(max_length=128, default="", db_index=True, verbose_name="字典排序键")
    description = models.TextField(blank=True, verbose_name="标签官方介绍")

    class Meta:
        verbose_name = "标签目录元数据"
        verbose_name_plural = "标签目录元数据"
        indexes = [
            models.Index(fields=["initial_group_rank", "initial", "sort_key"], name="tag_meta_initial_idx"),
        ]

    def __str__(self):
        return f"TagMeta({self.tag.name}: {self.initial})"

    @classmethod
    def get_or_create_for_tag(cls, tag: Tag) -> "TagDirectoryMetadata":
        """安全获取或自动计算补齐标签目录元数据"""
        meta = cls.objects.filter(tag=tag).first()
        if meta:
            return meta
        init_char = get_pinyin_initial(tag.name)
        group_rank = 0 if "A" <= init_char <= "Z" else 1
        meta, _ = cls.objects.get_or_create(
            tag=tag,
            defaults={
                "initial": init_char,
                "initial_group_rank": group_rank,
                "sort_key": tag.name.lower(),
            },
        )
        return meta


class TagIndexPage(RoutablePageMixin, CustomBasePage):
    """
    全站标签大厅与跨类型检索聚合中心。
    继承 RoutablePageMixin 提供 /tags/ 与 /tags/<slug>/ 优雅 RESTful 路由；
    继承 CustomBasePage 享有 30 天原树受控回收站保护与防误删门禁。
    """
    allow_comments = False
    intro = models.TextField(blank=True, verbose_name="标签大厅导语介绍")

    parent_page_types = ["home.HomePage"]
    subpage_types = []
    max_count_per_parent = 1

    content_panels = Page.content_panels + [
        FieldPanel("intro"),
    ]

    class Meta:
        verbose_name = "全站标签大厅页"
        verbose_name_plural = "全站标签大厅页"

    def clean(self):
        super().clean()
        if not self.slug:
            self.slug = "tags"

    def can_copy(self):
        return False

    def _enforce_entry_guard(self, request):
        """统一入口权限与生命周期守卫 (BLK-01)"""
        is_preview = bool(
            getattr(request, "_is_tag_preview", False)
            or getattr(request, "is_preview", False)
            or getattr(request, "in_preview_panel", False)
        )
        if (not getattr(self, "live", True)) and (not is_preview):
            raise Http404(f"页面 '{self.title}' 尚未发布或已被下架")
        if getattr(self, "is_recycled", False):
            raise Http404(f"页面 '{self.title}' 当前处于回收站中")

        # 检查 Wagtail 权限限制
        restrictions = self.get_view_restrictions()
        if restrictions.exists() and not is_preview:
            for restriction in restrictions:
                if not restriction.accept_runner(request):
                    from django.core.exceptions import PermissionDenied
                    raise PermissionDenied("当前标签大厅受私有权限限制。")

    def serve_preview(self, request, mode_name):
        """Wagtail 8.1 后台预览入口防崩溃守卫 (BLK-01)"""
        request.is_preview = True
        request._is_tag_preview = True
        self._enforce_entry_guard(request)
        return super().serve_preview(request, mode_name)

    def serve(self, request, *args, **kwargs):
        """前台入口：必须先执行权限守卫，再走 RoutablePageMixin 分发 (BLK-01)"""
        self._enforce_entry_guard(request)
        view = kwargs.get("view")
        if view is None and args and callable(args[0]):
            view = args[0]

        if view is None:
            # 当通过 page.serve(request) 直接调用时，根据 request.path 自动解析并路由
            url_path = self.get_url(request) if hasattr(self, "get_url") else self.url_path
            rel_path = request.path
            subpath = "/"
            if rel_path.startswith(url_path):
                subpath = "/" + rel_path[len(url_path):]
            elif rel_path.startswith(self.url_path):
                subpath = "/" + rel_path[len(self.url_path):]

            if not subpath.startswith("/"):
                subpath = "/" + subpath
            if not subpath.endswith("/"):
                subpath = subpath + "/"

            try:
                view, v_args, v_kwargs = self.resolve_subpage(subpath)
                args = v_args
                kwargs = v_kwargs
            except Http404:
                clean_seg = subpath.strip("/")
                if clean_seg:
                    view = self.detail_view
                    args = [clean_seg]
                    kwargs = {}
                else:
                    view = self.directory_view
                    args = []
                    kwargs = {}

            return view(request, *args, **kwargs)

        return super().serve(request, *args, **kwargs)

    @path("", name="directory")
    def directory_view(self, request):
        """模式一：全站标签大厅 (Top 20 热门云 + A-Z 分组 + 即时搜索)"""
        from taxonomy.services.directory import TagDirectoryService
        view_model = TagDirectoryService.get_tag_directory(request=request, index_page=self)
        is_ajax = bool(
            request.headers.get("X-Listing-Response") == "fragment-v1"
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.GET.get("format") == "partial"
        )
        if is_ajax:
            html = render_to_string(
                "taxonomy/partials/_tag_directory.html",
                {"view_model": view_model, "page": self, "request": request},
                request=request,
            )
            return JsonResponse({
                "html": html,
                "total_count": view_model.total_count,
                "canonical_url": self.get_url(request),
            })

        return self.render(
            request,
            context_overrides={"view_model": view_model, "mode": "directory"},
            template="taxonomy/tag_index_page.html",
        )

    @path("<str:tag_slug>/", name="detail")
    def detail_view(self, request, tag_slug):
        """模式二：单标签跨类型检索页 (多类型分栏 Tab + 深度检索 + 友好导流)"""
        from taxonomy.services.directory import TagDetailService
        detail_data = TagDetailService.get_tag_detail(request=request, index_page=self, tag_slug=tag_slug)

        is_ajax = bool(
            request.headers.get("X-Listing-Response") == "fragment-v1"
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.GET.get("format") == "partial"
        )

        if detail_data["mode"] == "missing":
            # 模式三：缺失友好导流 (BLK-05: 200 OK 友好建议，绝不暴露私有标签名称)
            if is_ajax:
                html = render_to_string(
                    "taxonomy/partials/_tag_missing.html",
                    {"data": detail_data, "page": self, "request": request},
                    request=request,
                )
                return JsonResponse({"html": html, "mode": "missing"})
            return self.render(
                request,
                context_overrides={"data": detail_data, "mode": "missing"},
                template="taxonomy/tag_index_page.html",
            )

        # 正常详情模式
        if is_ajax:
            html = render_to_string(
                "taxonomy/partials/_tag_detail.html",
                {"data": detail_data, "page": self, "request": request},
                request=request,
            )
            vm = detail_data["view_model"]
            return JsonResponse({
                "html": html,
                "total_count": vm.total_count,
                "canonical_url": detail_data["canonical_url"],
                "facets": vm.facets,
                "next_cursor": vm.next_cursor,
                "previous_cursor": vm.previous_cursor,
                "active_type": detail_data["active_type"],
            })

        return self.render(
            request,
            context_overrides={"data": detail_data, "mode": "detail"},
            template="taxonomy/tag_index_page.html",
        )
