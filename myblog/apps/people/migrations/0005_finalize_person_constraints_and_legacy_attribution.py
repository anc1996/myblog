import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("people", "0004_backfill_person_slugs"),
    ]

    operations = [
        migrations.AlterField(
            model_name="person",
            name="slug",
            field=models.SlugField(
                allow_unicode=False,
                editable=False,
                help_text="全站作者专栏与订阅稳定地址标识，生成后改名不自动变化",
                max_length=96,
                unique=True,
                verbose_name="规范Slug",
            ),
        ),
        migrations.AddIndex(
            model_name="person",
            index=models.Index(
                fields=["initial_group_rank", "initial", "sort_key", "id"],
                name="people_person_dir_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="person",
            constraint=models.CheckConstraint(
                condition=~Q(slug=""),
                name="people_person_slug_nonempty_chk",
            ),
        ),
        migrations.AddConstraint(
            model_name="person",
            constraint=models.CheckConstraint(
                condition=(
                    Q(is_system_anonymous=True, slug="anonymous", linked_user__isnull=True, email="")
                    | (Q(is_system_anonymous=False) & ~Q(slug="anonymous"))
                ),
                name="people_person_sys_anon_chk",
            ),
        ),
        migrations.CreateModel(
            name="LegacyAuthorAttribution",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("page_id", models.IntegerField(db_index=True, verbose_name="页面ID")),
                ("revision_id", models.IntegerField(unique=True, verbose_name="正式Revision ID")),
                ("approved_by_id", models.IntegerField(blank=True, null=True, verbose_name="核准人ID")),
                ("approved_at", models.DateTimeField(auto_now_add=True, verbose_name="核准时间")),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legacy_attributions",
                        to="people.person",
                        verbose_name="核准作者",
                    ),
                ),
            ],
            options={
                "verbose_name": "历史版本署名核准",
                "verbose_name_plural": "历史版本署名核准",
                "ordering": ["-approved_at"],
            },
        ),
    ]
