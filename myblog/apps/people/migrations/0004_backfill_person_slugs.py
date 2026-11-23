import uuid
from django.db import migrations
from people.author_identity import (
    build_unique_slug_with_suffix,
    compute_initial_and_sort_key,
    generate_base_slug,
)


def backfill_person_slugs_and_anonymous(apps, schema_editor):
    Person = apps.get_model("people", "Person")
    db_alias = schema_editor.connection.alias

    # 1. 先确保内置「佚名」名片存在，预留 slug='anonymous'
    anon_init, anon_rank, anon_sort = compute_initial_and_sort_key("佚名")
    if not Person.objects.using(db_alias).filter(slug="anonymous").exists():
        Person.objects.using(db_alias).create(
            public_id=uuid.uuid4(),
            name="佚名",
            slug="anonymous",
            initial=anon_init,
            initial_group_rank=anon_rank,
            sort_key=anon_sort,
            is_system_anonymous=True,
            bio="未单独署名的站点推送与资料归档",
            email="",
            linked_user=None,
        )
    else:
        Person.objects.using(db_alias).filter(slug="anonymous").update(
            is_system_anonymous=True,
            linked_user=None,
            email="",
            initial=anon_init,
            initial_group_rank=anon_rank,
            sort_key=anon_sort,
        )

    # 2. 按主键顺序回填存量人物的 slug 与拼音排序字段
    used_slugs = set(
        Person.objects.using(db_alias)
        .exclude(slug__isnull=True)
        .exclude(slug="")
        .values_list("slug", flat=True)
    )
    used_slugs.add("anonymous")

    last_id = 0
    batch_size = 500
    while True:
        batch = list(
            Person.objects.using(db_alias)
            .filter(id__gt=last_id)
            .order_by("id")[:batch_size]
        )
        if not batch:
            break
        for person in batch:
            last_id = person.id
            if person.is_system_anonymous:
                continue
            init_char, rank, sort_key = compute_initial_and_sort_key(person.name)
            update_fields = []
            if person.initial != init_char:
                person.initial = init_char
                update_fields.append("initial")
            if person.initial_group_rank != rank:
                person.initial_group_rank = rank
                update_fields.append("initial_group_rank")
            if person.sort_key != sort_key:
                person.sort_key = sort_key
                update_fields.append("sort_key")

            if not person.slug:
                base_slug = generate_base_slug(person.name)
                chosen = None
                for attempt in range(0, 12):
                    cand = build_unique_slug_with_suffix(base_slug, person.public_id, attempt=attempt)
                    if cand not in used_slugs:
                        chosen = cand
                        break
                if not chosen:
                    chosen = f"person-{uuid.uuid4().hex[:16]}"
                used_slugs.add(chosen)
                person.slug = chosen
                update_fields.append("slug")

            if update_fields:
                person.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ("people", "0003_person_slug_fields"),
    ]

    operations = [
        migrations.RunPython(backfill_person_slugs_and_anonymous, migrations.RunPython.noop),
    ]
