"""
受控同义词扩展服务 (模式 C 核心依赖)。

落实契约：
1. 人工审核的方向与领域受控同义词，绝不无限制递归发散；
2. 扩展每个必需概念组为 [原词, 替代词1, 替代词2]；
3. 仅在模式 B 命中数少于门槛 (默认10条) 且 expand!='off' 时自动触发模式 C。
"""

CONTROLLED_SYNONYMS: dict[str, list[str]] = {
    "postgres": ["postgresql"],
    "postgresql": ["postgres"],
    "py": ["python"],
    "python": ["py"],
    "golang": ["go"],
    "go": ["golang"],
    "k8s": ["kubernetes"],
    "kubernetes": ["k8s"],
    "js": ["javascript"],
    "javascript": ["js"],
    "ts": ["typescript"],
    "typescript": ["ts"],
    "orm": ["对象关系映射"],
    "对象关系映射": ["orm"],
    "s3": ["rustfs"],
    "rustfs": ["s3"],
    "db": ["数据库"],
    "数据库": ["db"],
    "techpostgres18": ["postgresql", "postgres"],
    "techdjango61": ["django"],
    "techcpp": ["c++"],
}


def expand_terms(terms: list[str]) -> tuple[list[list[str]], bool]:
    """
    对查询词元列表进行受控同义词扩展。
    返回：
    - concept_groups: 概念组列表，每个组内为 [原词, 同义词...]（组内为 OR 关系）
    - expansion_used: 是否实际使用了同义词扩展
    """
    concept_groups: list[list[str]] = []
    expansion_used = False

    for t in terms:
        t_lower = t.lower()
        synonyms = CONTROLLED_SYNONYMS.get(t_lower, [])
        if synonyms:
            expansion_used = True
            # 原词优先，最多保留 3 个同义词
            group = [t] + [s for s in synonyms if s != t][:3]
            concept_groups.append(group)
        else:
            concept_groups.append([t])

    return concept_groups, expansion_used
