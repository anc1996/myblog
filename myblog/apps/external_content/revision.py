"""
PostgreSQL Wagtail Revision 与 MongoDB 外置版本指针绑定及解析服务。

实现核心规则：
1. Revision JSON 中登记 _external_field_pointers 列表；
2. 读者请求时，由有效 Revision (如 live_revision) 决定读取哪个版本的 Mongo 内容；
3. 编辑人员修改草稿时生成新版本快照并绑定到新草稿 Revision，绝不污染已上线的正式版本内容。
"""

import json
from typing import Any
from uuid import UUID
from wagtail.models import Revision

from external_content.contracts import FieldVersionPointer
from external_content.store import MongoContentStore
from content_lifecycle.access import AccessContext, AccessMode


EXTERNAL_POINTERS_KEY = "_external_field_pointers"


def bind_pointers_to_revision_content(content_dict: dict[str, Any], pointers: list[FieldVersionPointer]) -> dict[str, Any]:
    """在 Revision content 字典中登记外置字段指针"""
    pointers_data = [
        {
            "document_uuid": str(p.document_uuid),
            "field_name": p.field_name,
            "version_id": p.version_id,
            "schema_version": p.schema_version,
            "content_hash": p.content_hash,
        }
        for p in pointers
    ]
    content_dict[EXTERNAL_POINTERS_KEY] = pointers_data
    return content_dict


def extract_pointers_from_revision(revision: Revision) -> dict[str, FieldVersionPointer]:
    """从 Revision 实例反序列化外置字段指针字典 {field_name: FieldVersionPointer}"""
    content = revision.content if isinstance(revision.content, dict) else json.loads(revision.content)
    pointers_list = content.get(EXTERNAL_POINTERS_KEY, [])
    result: dict[str, FieldVersionPointer] = {}
    for item in pointers_list:
        ptr = FieldVersionPointer(
            document_uuid=UUID(item["document_uuid"]),
            field_name=item["field_name"],
            version_id=item["version_id"],
            schema_version=item["schema_version"],
            content_hash=item["content_hash"],
        )
        result[ptr.field_name] = ptr
    return result


def get_effective_page_content(
    page,
    access_context: AccessContext | None = None,
    store: MongoContentStore | None = None,
) -> dict[str, Any]:
    """
    统一页面内容解析服务：
    1. 若访问模式为 PUBLIC: 严格按 page.live_revision 取版本；若未发布返回不可见错误；
    2. 若访问模式为 PREVIEW_DRAFT: 取得 page.get_latest_revision() 取最新草稿版本；
    3. 若访问模式为 PREVIEW_REVISION: 取指定的 target_revision_id 版本；
    4. 按该 Revision 记录的 version_id 与 hash 精确从 Mongo 提取 intro 和 body！
    """
    if access_context is None:
        access_context = AccessContext(mode=AccessMode.PUBLIC)

    if store is None:
        store = MongoContentStore()

    # 确定生效的目标 Revision
    if access_context.mode == AccessMode.PUBLIC:
        if not page.live or not page.live_revision_id:
            return {"error": "page_not_published", "page_id": page.id}
        effective_revision = page.live_revision
    elif access_context.mode == AccessMode.PREVIEW_REVISION and access_context.target_revision_id:
        effective_revision = Revision.objects.get(id=access_context.target_revision_id)
    else:
        effective_revision = page.get_latest_revision() or page.live_revision

    if not effective_revision:
        return {"error": "no_revision_found", "page_id": page.id}

    pointers = extract_pointers_from_revision(effective_revision)
    doc_uuid = getattr(page, "document_uuid", None)

    intro_payload: Any = []
    body_payload: Any = []

    if "intro" in pointers:
        p_intro = pointers["intro"]
        res_intro = store.get_field_version(
            document_uuid=p_intro.document_uuid,
            field_name="intro",
            version_id=p_intro.version_id,
            expected_hash=p_intro.content_hash,
        )
        intro_payload = res_intro.payload

    if "body" in pointers:
        p_body = pointers["body"]
        res_body = store.get_field_version(
            document_uuid=p_body.document_uuid,
            field_name="body",
            version_id=p_body.version_id,
            expected_hash=p_body.content_hash,
        )
        body_payload = res_body.payload

    return {
        "page_id": page.id,
        "document_uuid": str(doc_uuid) if doc_uuid else "",
        "revision_id": effective_revision.id,
        "title": page.title,
        "authored_on": getattr(page, "authored_on", None),
        "intro": intro_payload,
        "body": body_payload,
        "is_public_version": effective_revision.id == page.live_revision_id,
    }
