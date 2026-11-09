from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """
    网站后台账户模型。
    负责后台管理登录、用户组（Group）与权限（Permission）管理。
    对外公开的作者等人物资料由独立的 people.Person 管理，两者严格解耦。
    """

    class Meta:
        verbose_name = "后台用户"
        verbose_name_plural = "后台用户"
