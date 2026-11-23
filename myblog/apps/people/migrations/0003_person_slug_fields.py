from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("people", "0002_person_linked_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="person",
            name="initial",
            field=models.CharField(db_index=True, default="#", max_length=1, verbose_name="首字母分组"),
        ),
        migrations.AddField(
            model_name="person",
            name="initial_group_rank",
            field=models.SmallIntegerField(db_index=True, default=1, verbose_name="分组桶序(0=A-Z,1=#)"),
        ),
        migrations.AddField(
            model_name="person",
            name="is_system_anonymous",
            field=models.BooleanField(default=False, editable=False, verbose_name="系统内置佚名标记"),
        ),
        migrations.AddField(
            model_name="person",
            name="slug",
            field=models.SlugField(
                allow_unicode=False,
                editable=False,
                help_text="全站作者专栏与订阅稳定地址标识，生成后改名不自动变化",
                max_length=96,
                null=True,
                unique=True,
                verbose_name="规范Slug",
            ),
        ),
        migrations.AddField(
            model_name="person",
            name="sort_key",
            field=models.CharField(db_index=True, default="", max_length=255, verbose_name="排序键"),
        ),
    ]
