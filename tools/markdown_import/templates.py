"""
为客户端提供一键复制的 YAML Front Matter 标准模板。
"""

TEMPLATES = {
    "blog_page": """---
page_type: blog_page
title: 请填写文章标题（可省略：自动抓首行 # 一级标题或由 AI 生成）
date: 2026-11-22
tags: [PostgreSQL, Wagtail]
cover: images/cover.png
intro: 请填写文章简介（可省略：导入时一键由 AI 自动讲解生成）
---

在这里写正文，支持表格、公式、Mermaid 与本地图片 ![架构图](images/arch.png)
""",
    "guide_page": """---
page_type: guide_page
title: 从零配置技术专栏教程（可省略）
date: 2026-11-22
topic: 存储与数据库
difficulty: intermediate
estimated_minutes: 25
tags: [RustFS, Django]
cover: images/cover.png
summary: 请填写指南摘要（可省略：可由 AI 生成）
---

前置背景说明。

:::step 1 | 第一步标题
这里写第一步操作说明。
:::
""",
    "project_page": """---
page_type: project_page
title: 开源项目名称（可省略）
date: 2026-11-22
project_status: maintained
primary_language: Python
license_name: MIT
repo_url: https://github.com/anc1996/myblog
demo_url: https://example.com
tags: [Django, Wagtail]
cover: images/banner.png
summary: 请填写项目摘要说明（可省略：可由 AI 生成）
---

这里写项目架构与功能介绍。
""",
}


def render_front_matter_template(type_key: str) -> str:
    return TEMPLATES.get(type_key, TEMPLATES["blog_page"])
