import uuid
from django.db import models
from wagtail.images.models import AbstractImage, AbstractRendition, Image
from wagtail.documents.models import AbstractDocument, Document
from wagtail.snippets.models import register_snippet


class VisibilityChoices(models.TextChoices):
    PUBLIC = "public", "公开"
    RESTRICTED = "restricted", "受限"


class CustomImage(AbstractImage):
    """
    全站公共图片模型。
    继承 Wagtail 原生 AbstractImage，保留原生图片裁切、焦点、集合与选择器能力。
    增加全站稳定资源标识（public_id）与默认访问策略（visibility）。
    """
    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name="公开资源标识",
        help_text="跨页面、跨版本引用时使用的稳定不可变唯一标识",
    )
    visibility = models.CharField(
        max_length=16,
        choices=VisibilityChoices.choices,
        default=VisibilityChoices.RESTRICTED,
        db_index=True,
        verbose_name="访问控制策略",
        help_text="默认受限（restricted）；只有经授权批准后才可改为公开（public）",
    )
    storage_key = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        verbose_name="存储对象Key",
        help_text="RustFS 对象存储中的不可变对象键",
    )

    admin_form_fields = Image.admin_form_fields

    class Meta(AbstractImage.Meta):
        verbose_name = "图片"
        verbose_name_plural = "图片"


class CustomRendition(AbstractRendition):
    """
    全站公共派生图片（缩略图）模型。
    按尺寸或滤镜规则从原图衍生生成，严格指向 CustomImage。
    """
    image = models.ForeignKey(
        CustomImage,
        on_delete=models.CASCADE,
        related_name="renditions",
        verbose_name="原图",
    )

    class Meta:
        unique_together = (("image", "filter_spec", "focal_point_key"),)
        verbose_name = "派生图片"
        verbose_name_plural = "派生图片"


class CustomDocument(AbstractDocument):
    """
    全站公共文档模型。
    继承 Wagtail 原生 AbstractDocument，保留原生文件上传、文档选择器与集合能力。
    增加全站稳定资源标识（public_id）与访问控制策略（visibility）。
    """
    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name="公开资源标识",
        help_text="跨页面、跨版本引用时使用的稳定不可变唯一标识",
    )
    visibility = models.CharField(
        max_length=16,
        choices=VisibilityChoices.choices,
        default=VisibilityChoices.RESTRICTED,
        db_index=True,
        verbose_name="访问控制策略",
        help_text="默认受限（restricted）；由统一网关鉴权后交付",
    )
    storage_key = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        verbose_name="存储对象Key",
    )

    admin_form_fields = Document.admin_form_fields

    class Meta(AbstractDocument.Meta):
        verbose_name = "文档"
        verbose_name_plural = "文档"


@register_snippet
class Audio(models.Model):
    """
    全站公共音频模型。
    供全站所有业务 Page 统一引用，提供基本的受控播放与分段交付能力。
    """
    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name="公开资源标识",
    )
    title = models.CharField(max_length=255, verbose_name="音频标题")
    file = models.FileField(upload_to="audio/", verbose_name="音频文件")
    visibility = models.CharField(
        max_length=16,
        choices=VisibilityChoices.choices,
        default=VisibilityChoices.RESTRICTED,
        db_index=True,
        verbose_name="访问控制策略",
    )
    storage_key = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        verbose_name="存储对象Key",
    )
    duration = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="时长(秒)",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "音频"
        verbose_name_plural = "音频"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


@register_snippet
class Video(models.Model):
    """
    全站公共视频模型。
    供全站所有业务 Page 统一引用，支持封面海报图、受控播放与 Range 分段续传。
    """
    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name="公开资源标识",
    )
    title = models.CharField(max_length=255, verbose_name="视频标题")
    file = models.FileField(upload_to="video/", verbose_name="视频文件")
    poster = models.ForeignKey(
        CustomImage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="封面海报",
    )
    visibility = models.CharField(
        max_length=16,
        choices=VisibilityChoices.choices,
        default=VisibilityChoices.RESTRICTED,
        db_index=True,
        verbose_name="访问控制策略",
    )
    storage_key = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        verbose_name="存储对象Key",
    )
    duration = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="时长(秒)",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "视频"
        verbose_name_plural = "视频"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
