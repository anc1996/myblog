"""
全站公开人物资料模型 (Person)。

独立于 identity.User 管理后台登录账户：
1. 网站公开作者、审校等人物无需登录后台；
2. 修改作者公开资料不影响后台用户权限，两者严格解耦；
3. 注册为 Wagtail Snippet，方便在管理后台录入与选择。
"""

import uuid
from django.conf import settings
from django.db import models
from wagtail.snippets.models import register_snippet
from wagtail.admin.panels import FieldPanel

from media_library.models import CustomImage


@register_snippet
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
    avatar = models.ForeignKey(
        CustomImage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="公开头像",
    )
    bio = models.TextField(blank=True, verbose_name="公开简介")
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
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    panels = [
        FieldPanel("name"),
        FieldPanel("avatar"),
        FieldPanel("bio"),
        FieldPanel("email"),
        FieldPanel("linked_user", permission="identity.change_user"),
    ]

    class Meta:
        verbose_name = "公开人物/作者"
        verbose_name_plural = "公开人物/作者"
        ordering = ["name"]

    def __str__(self):
        return self.name
