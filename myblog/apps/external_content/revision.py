"""
PostgreSQL Wagtail Revision 与 MongoDB 外置版本指针绑定及解析服务。

【核心设计架构教学】：
在 Wagtail CMS 架构中，Revision（页面历史版本表）记录了编辑者每一次点击“保存草稿”或“发布”时的页面完整快照。
本模块是连接“PostgreSQL 关系型主库”与“MongoDB 非关系型脱水存储”的桥梁：
1. 【登记指针】：页面保存 Revision 时，将 MongoDB 中不可变正文的版本元数据（FieldVersionPointer）
   打包写入 Revision JSON 的 `_external_field_pointers` 列表中（体积仅几十字节）；
2. 【严格分流】：根据访问身份（普通公网读者 vs 后台管理员预览草稿 vs 查看特定历史对比），
   精确决定以哪个 Revision 作为“生效权威版本”；
3. 【水合还原】：依据生效 Revision 绑定的 version_id 和 SHA-256 哈希值，从 MongoDB 取出对应正文，
   杜绝草稿未发布外泄，同时实现线上正式版本与后台草稿版本的绝对隔离。
"""

import json
from typing import Any
from uuid import UUID
from wagtail.models import Revision

from external_content.contracts import FieldVersionPointer
from external_content.store import MongoContentStore
from content_lifecycle.access import AccessContext, AccessMode

# 存储在 Wagtail Revision.content 字典中的私有外置指针键名
# 变量说明：
# - 采用下划线 `_` 开头命名，避免与用户自定义的页面字段名（如 title, body 等）产生命名冲突
EXTERNAL_POINTERS_KEY = "_external_field_pointers"


def bind_pointers_to_revision_content(
    content_dict: dict[str, Any],
    pointers: list[FieldVersionPointer],
) -> dict[str, Any]:
    """
    在 Wagtail Revision 的内容字典中登记外置字段指针。

    【参数与变量说明】：
    - content_dict (dict[str, Any]): 页面保存时序列化出的字段字典（准备写入 PostgreSQL 的 revision.content JSON 列）。
    - pointers (list[FieldVersionPointer]): 当前页面所有外置字段的指针对象列表（如 body 和 intro 的指针）。
    - pointers_data (list[dict]): 局部变量，将 Python 的 dataclass 对象转换为纯基础数据类型的字典列表，
      包含：
        * document_uuid: 页面逻辑唯一标识（转为字符串）；
        * field_name: 字段名（'body' 或 'intro'）；
        * version_id: Mongo 中的版本唯一标识符；
        * schema_version: 结构模式版本号；
        * content_hash: 正文内容 SHA-256 哈希值。
    - 返回值 (dict[str, Any]): 注入了 `_external_field_pointers` 的原始 content_dict，可安全序列化写入数据库。
    """
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
    """
    从指定 Wagtail Revision 实例中反序列化提取外置字段指针。

    【参数与变量说明】：
    - revision (Revision): Wagtail 历史版本模型实例（对应 PostgreSQL 中的一条版本快照记录）。
    - content (dict): 局部变量，从 revision.content 提取的字典（兼容已经是 dict 或 JSON 字符串形式）。
    - pointers_list (list[dict]): 局部变量，从 content 字典中提取出的 `_external_field_pointers` 原始列表。
    - result (dict[str, FieldVersionPointer]): 局部变量，重构后的映射字典，以字段名为 Key（例如 result['body']），
      极大方便后续按字段快速检索指针对象。
    - item (dict): 循环迭代变量，单条指针的原始字典数据。
    - ptr (FieldVersionPointer): 局部变量，由原始数据重新还原生成的强类型不可变指针对象。
    - 返回值 (dict[str, FieldVersionPointer]): 字段名到指针对象的字典映射。
    """
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
    【统一页面内容解析服务】—— 全站内容水合（Hydration）与安全准入决策中心。

    【教学思考】：
    当一个 HTTP 请求到来时，我们怎么知道应该把哪一份正文展示给客户端？
    - 是公网读者（读 page.live_revision）？
    - 还是后台管理员在预览未发布的草稿（读 page.get_latest_revision()）？
    - 或者是管理员在审查某个历史版本对比（读指定 target_revision_id）？
    本函数集中负责这套裁决逻辑，并从 MongoDB 提取真实正文组装返回。

    【参数与变量详细解析】：
    - 入参 page: 当前被访问的 Wagtail 页面实例（继承 CustomBasePage 的具体页面模型，如 BlogPage）。
    - 入参 access_context (AccessContext | None): 访问上下文契约对象，用于标识请求者的访问模式。
      默认构造为 AccessMode.PUBLIC（公网读者只读已发布版本）。
    - 入参 store (MongoContentStore | None): MongoDB 外部内容持久化存储驱动实例。
      若外部未注入连接池，则在本地安全实例化默认的 MongoContentStore。

    - 局部变量 effective_revision (Revision): 本次裁决最终选定作为内容权威的生效 Revision 对象。
    - 局部变量 pointers (dict[str, FieldVersionPointer]): 从生效 Revision 提取出的各字段指针映射表。
    - 局部变量 doc_uuid (UUID | None): 页面的全局逻辑 UUID，页面移动改名均保持不变。
    - 局部变量 intro_payload (Any): 导语字段从 MongoDB 检索水合出的积木内容列表（默认为 []）。
    - 局部变量 body_payload (Any): 正文字段从 MongoDB 检索水合出的积木内容列表（默认为 []）。
    - 局部变量 p_intro / p_body (FieldVersionPointer): 对应的字段版本指针对象。
    - 局部变量 res_intro / res_body (FieldVersionPayload): 从 MongoContentStore 获取并核验过哈希的完整版本载荷。

    - 返回值 (dict[str, Any]): 组装完成的页面完整内容字典，供上层视图或模板渲染。
    """
    # 1. 默认准入策略初始化：未指定时默认为严格公网访问模式
    if access_context is None:
        access_context = AccessContext(mode=AccessMode.PUBLIC)

    # 2. 存储驱动初始化
    if store is None:
        store = MongoContentStore()

    # 3. 核心决策分支：根据访问身份裁决“生效 Revision”
    if access_context.mode == AccessMode.PUBLIC:
        # 【安全红线】：公网读者访问时，页面必须处于 live 状态且拥有正式发布版本号
        if not page.live or not page.live_revision_id:
            return {"error": "page_not_published", "page_id": page.id}
        effective_revision = page.live_revision
    elif access_context.mode == AccessMode.PREVIEW_REVISION and access_context.target_revision_id:
        # 【后台历史审计】：管理员指定查看某个具体历史版本（例如在后台执行差异比对）
        effective_revision = Revision.objects.get(id=access_context.target_revision_id)
    else:
        # 【后台草稿预览】：编辑者预览最新草稿，优先取最新草稿 revision，若无草稿回退至当前发布 revision
        effective_revision = page.get_latest_revision() or page.live_revision

    # 若最终找不到任何有效 Revision，返回明确错误
    if not effective_revision:
        return {"error": "no_revision_found", "page_id": page.id}

    # 4. 从生效 Revision 中解析出版本指针字典
    pointers = extract_pointers_from_revision(effective_revision)
    doc_uuid = getattr(page, "document_uuid", None)

    intro_payload: Any = []
    body_payload: Any = []

    # 5. 按生效指针从 MongoDB 提取导语（intro）内容并比对内容哈希防篡改
    if "intro" in pointers:
        p_intro = pointers["intro"]
        res_intro = store.get_field_version(
            document_uuid=p_intro.document_uuid,
            field_name="intro",
            version_id=p_intro.version_id,
            expected_hash=p_intro.content_hash,
        )
        intro_payload = res_intro.payload

    # 6. 按生效指针从 MongoDB 提取正文（body）内容并比对内容哈希防篡改
    if "body" in pointers:
        p_body = pointers["body"]
        res_body = store.get_field_version(
            document_uuid=p_body.document_uuid,
            field_name="body",
            version_id=p_body.version_id,
            expected_hash=p_body.content_hash,
        )
        body_payload = res_body.payload

    # 7. 组装并返回水合后的完整业务字典
    return {
        "page_id": page.id,                                                 # 页面在 PostgreSQL 中的主键 ID
        "document_uuid": str(doc_uuid) if doc_uuid else "",                 # 页面逻辑文档全局唯一 UUID
        "revision_id": effective_revision.id,                              # 当前展示正文所属的 Revision ID
        "title": page.title,                                               # 页面标题
        "authored_on": getattr(page, "authored_on", None),                # 成文日期
        "intro": intro_payload,                                            # 从 MongoDB 水合还原出的导语积木数据
        "body": body_payload,                                              # 从 MongoDB 水合还原出的正文积木数据
        "is_public_version": effective_revision.id == page.live_revision_id, # 标识当前展示的是否为线上正式版本
    }
