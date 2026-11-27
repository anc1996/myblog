"""
全站公开人物资料模型 (Person)。

独立于 identity.User 管理后台登录账户：
1. 网站公开作者、审校等人物无需登录后台；
2. 修改作者公开资料不影响后台用户权限，两者严格解耦；
3. 注册为受控 Wagtail Snippet，保护内置「佚名」名片与稳定 URL Slug。
"""

import uuid
import html
import re
from typing import Any
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils.safestring import mark_safe
from wagtail.admin.panels import FieldPanel
from wagtail.fields import RichTextField

from media_library.models import CustomImage
from people.author_identity import (
    build_unique_slug_with_suffix,
    compute_initial_and_sort_key,
    generate_base_slug,
    is_disallowed_ordinary_slug,
)

# 公开简介（非页面 Snippet）仅开放文本排版与站内/外链接，不开放图片/文档/嵌入以防绕过资产发布鉴权
PERSON_BIO_RICH_TEXT_FEATURES = [
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "code",
    "superscript",
    "subscript",
    "ol",
    "ul",
    "blockquote",
    "align-left",
    "align-center",
    "align-right",
    "link",
]


_BIO_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|ul|ol|blockquote|h[1-6])(?:\s[^>]*)?>", re.IGNORECASE)
_BIO_ANY_TAG_RE = re.compile(r"<[^>]+>")


def extract_person_bio_text(raw_bio: Any) -> str:
    """提取公开简介纯文本（供目录卡片摘要、搜索匹配与快照元数据共用）。"""
    if not raw_bio:
        return ""
    from wagtail.rich_text import RichText

    raw_html = raw_bio.source if isinstance(raw_bio, RichText) else str(raw_bio)
    # 块级标签保留空格分隔，行内标签（b/u/a/code 等）直接剥离，避免中文标点前出现多余空格
    spaced = _BIO_BLOCK_TAG_RE.sub(" ", raw_html)
    stripped = html.unescape(_BIO_ANY_TAG_RE.sub("", spaced))
    return re.sub(r"\s+", " ", stripped).strip()


def render_person_bio_html(raw_bio: Any) -> str:
    """展开 Wagtail 站内链接并经 nh3 白名单清洗，空段落（如 <p></p>）返回空串。"""
    if not raw_bio:
        return ""
    if not extract_person_bio_text(raw_bio):
        return ""
    from content_blocks.blocks.rich_text import render_richtext_field_html

    cleaned = render_richtext_field_html(raw_bio)
    return mark_safe(cleaned) if cleaned else ""


class Person(models.Model):
    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name="公开标识",
        help_text="全站统一稳定的人员唯一标识",
    )
    name = models.CharField(max_length=120, verbose_name="姓名")
    slug = models.SlugField(
        max_length=96,
        unique=True,
        editable=False,
        allow_unicode=False,
        verbose_name="规范Slug",
        help_text="全站作者专栏与订阅稳定地址标识，生成后改名不自动变化",
    )
    initial = models.CharField(
        max_length=1,
        default="#",
        db_index=True,
        verbose_name="首字母分组",
    )
    initial_group_rank = models.SmallIntegerField(
        default=1,
        db_index=True,
        verbose_name="分组桶序(0=A-Z,1=#)",
    )
    sort_key = models.CharField(
        max_length=255,
        default="",
        db_index=True,
        verbose_name="排序键",
    )
    is_system_anonymous = models.BooleanField(
        default=False,
        editable=False,
        verbose_name="系统内置佚名标记",
    )
    avatar = models.ForeignKey(
        CustomImage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="公开头像",
    )
    bio = RichTextField(
        blank=True,
        features=list(PERSON_BIO_RICH_TEXT_FEATURES),
        verbose_name="公开简介",
    )
    email = models.EmailField(blank=True, verbose_name="公开联系邮箱")
    linked_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="person_profile",
        verbose_name="关联后台账号",
        help_text="仅由管理员人工核验后手工绑定，用于创作后台自动预填作者署名，禁止自动认领",
    )
    show_public_comments = models.BooleanField(
        default=False,
        verbose_name="允许在作者名片公开聚合历史评论",
        help_text="仅对已绑定后台账号的非佚名作者生效；开启后将聚合该账号在公开页面下仍公开可见的历史评论（改绑或解绑账号时自动关闭）。",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    panels = [
        FieldPanel("name"),
        FieldPanel("avatar"),
        FieldPanel("bio"),
        FieldPanel("email"),
        FieldPanel("linked_user", permission="identity.change_user"),
        FieldPanel("show_public_comments", permission="identity.change_user"),
    ]

    class Meta:
        verbose_name = "公开人物/作者"
        verbose_name_plural = "公开人物/作者"
        ordering = ["name"]
        indexes = [
            models.Index(
                fields=["initial_group_rank", "initial", "sort_key", "id"],
                name="people_person_dir_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(slug=""),
                name="people_person_slug_nonempty_chk",
            ),
            models.CheckConstraint(
                condition=(
                    Q(is_system_anonymous=True, slug="anonymous", linked_user__isnull=True, email="")
                    | (Q(is_system_anonymous=False) & ~Q(slug="anonymous"))
                ),
                name="people_person_sys_anon_chk",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.is_system_anonymous:
            if self.linked_user_id is not None:
                raise ValidationError({"linked_user": "系统内置「佚名」名片禁止绑定后台账号。"})
            if (self.email or "").strip():
                raise ValidationError({"email": "系统内置「佚名」名片禁止设置公开邮箱。"})
            if self.show_public_comments:
                raise ValidationError({"show_public_comments": "系统内置「佚名」名片禁止开启历史评论聚合。"})
        elif self.show_public_comments and self.linked_user_id is None:
            raise ValidationError({"show_public_comments": "未绑定后台账号的作者不可开启历史评论聚合。"})

    def save(self, *args, **kwargs):
        if not self.public_id:
            self.public_id = uuid.uuid4()

        init_char, group_rank, sort_key = compute_initial_and_sort_key(self.name)
        self.initial = init_char
        self.initial_group_rank = group_rank
        self.sort_key = sort_key

        using = kwargs.get("using") or self._state.db or "default"
        if self.is_system_anonymous:
            self.linked_user = None
            self.show_public_comments = False
        elif self.linked_user_id is None:
            self.show_public_comments = False
        elif self.pk:
            prev_linked_id = (
                type(self)
                .objects.using(using)
                .filter(pk=self.pk)
                .values_list("linked_user_id", flat=True)
                .first()
            )
            if prev_linked_id != self.linked_user_id:
                self.show_public_comments = False

        update_fields = kwargs.get("update_fields")
        if update_fields is not None and not self.show_public_comments:
            uf = set(update_fields)
            if "linked_user" in uf or "linked_user_id" in uf or "is_system_anonymous" in uf:
                uf.add("show_public_comments")
                kwargs["update_fields"] = list(uf)

        if self.is_system_anonymous:
            self.slug = "anonymous"
            self.linked_user = None
            self.email = ""
            return super().save(*args, **kwargs)

        if self.slug:
            if self.slug == "anonymous" or is_disallowed_ordinary_slug(self.slug):
                raise ValidationError("普通公开人物不可占用系统保留或非法 Slug。")
            return super().save(*args, **kwargs)

        base_slug = generate_base_slug(self.name)
        for attempt in range(0, 8):
            candidate = build_unique_slug_with_suffix(base_slug, self.public_id, attempt=attempt)
            self.slug = candidate
            try:
                with transaction.atomic(using=using):
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                if type(self).objects.using(using).filter(slug=candidate).exclude(pk=self.pk).exists():
                    continue
                raise exc
        self.slug = f"person-{uuid.uuid4().hex[:16]}"
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_system_anonymous or self.slug == "anonymous":
            raise PermissionDenied("系统内置「佚名」名片受永久保护，禁止删除。")
        return super().delete(*args, **kwargs)


def get_or_create_anonymous_person(*, using: str = "default") -> Person:
    """幂等获取或创建全站内置「佚名」名片（slug='anonymous'），绝不覆盖管理员已修改的展示名/头像/简介。"""
    existing = Person.objects.using(using).filter(slug="anonymous").first()
    if existing is not None:
        return existing
    init_char, group_rank, sort_key = compute_initial_and_sort_key("佚名")
    obj, _ = Person.objects.using(using).get_or_create(
        slug="anonymous",
        defaults={
            "name": "佚名",
            "bio": "<p>未单独署名的站点推送与资料归档</p>",
            "email": "",
            "linked_user": None,
            "is_system_anonymous": True,
            "initial": init_char,
            "initial_group_rank": group_rank,
            "sort_key": sort_key,
        },
    )
    return obj


class LegacyAuthorAttribution(models.Model):
    """
    受控历史正式版本署名核准表。
    仅通过管理员审定与受控命令按 revision_id 幂等写入，不自动从当前 owner 推导。
    """
    page_id = models.IntegerField(db_index=True, verbose_name="页面ID")
    revision_id = models.IntegerField(unique=True, verbose_name="正式Revision ID")
    person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="legacy_attributions",
        verbose_name="核准作者",
    )
    approved_by_id = models.IntegerField(null=True, blank=True, verbose_name="核准人ID")
    approved_at = models.DateTimeField(auto_now_add=True, verbose_name="核准时间")

    class Meta:
        verbose_name = "历史版本署名核准"
        verbose_name_plural = "历史版本署名核准"
        ordering = ["-approved_at"]

    def __str__(self):
        return f"LegacyAuthorAttribution(page={self.page_id}, rev={self.revision_id}, person={self.person_id})"
