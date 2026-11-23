import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q

from people.author_identity import CANONICAL_AUTHOR_ROLES


class Migration(migrations.Migration):

    dependencies = [
        ("discovery", "0002_archivecontribution_archivecountstate_and_more"),
        ("people", "0005_finalize_person_constraints_and_legacy_attribution"),
    ]

    operations = [
        migrations.CreateModel(
            name="PublishedSearchAuthor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("person_id", models.BigIntegerField(db_index=True, verbose_name="公开人物主键")),
                ("author_name", models.CharField(max_length=120, verbose_name="发布时作者姓名快照")),
                ("author_slug", models.CharField(db_index=True, max_length=96, verbose_name="规范作者Slug")),
                ("role", models.CharField(db_index=True, max_length=32, verbose_name="归一化贡献角色")),
                ("sort_order", models.SmallIntegerField(default=0, verbose_name="署名排序")),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="search_authors",
                        to="discovery.publishedsearchdocument",
                        verbose_name="所属搜索文档",
                    ),
                ),
            ],
            options={
                "verbose_name": "搜索文档作者",
                "verbose_name_plural": "搜索文档作者",
                "indexes": [
                    models.Index(fields=["author_slug", "document"], name="disc_author_slug_doc_idx"),
                    models.Index(fields=["author_slug", "role", "document"], name="disc_author_role_doc_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=["document", "author_slug"], name="uniq_disc_doc_author_slug"),
                    models.CheckConstraint(condition=Q(role__in=CANONICAL_AUTHOR_ROLES), name="disc_author_role_enum_chk"),
                    models.CheckConstraint(condition=Q(sort_order__gte=0), name="disc_author_sort_nonneg_chk"),
                    models.CheckConstraint(
                        condition=(
                            Q(role="anonymous", author_slug="anonymous")
                            | (~Q(role="anonymous") & ~Q(author_slug="anonymous"))
                        ),
                        name="disc_author_anon_pair_chk",
                    ),
                ],
            },
        ),
    ]
