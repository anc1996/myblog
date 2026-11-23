"""
本地 Markdown 文件与资料目录安全扫描器。

安全保证：
1. 选择目录时仅扫描该目录顶层 `.md`/`.markdown` 文件（防止误扫子目录草稿）；
2. 解析正文引用的相对资源路径时，逐个检查真实路径严格落在所选根目录内，
   坚决阻断 `..` 越界、盘符、UNC 以及指向目录外的符号链接/junction 逃逸；
3. 计算实际字节数与 SHA-256，绝不向服务端暴露本机绝对目录树。
"""

from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from markdown_import.parser import (
    extract_local_media_refs,
    parse_front_matter,
    validate_relative_asset_path,
)


@dataclass(frozen=True)
class LocalAssetFile:
    """已通过本地路径安全检查的配套图片或文档文件。"""
    asset_key: str
    source_path: str
    media_type: str
    local_path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ScannedMarkdownSource:
    """单篇本地 Markdown 扫描结果。"""
    root_dir: Path
    markdown_path: Path
    source_name: str
    raw_text: str
    assets: tuple[LocalAssetFile, ...] = ()
    missing_assets: tuple[str, ...] = ()
    blocked_paths: tuple[str, ...] = ()


def _sha256_file(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
    return total, h.hexdigest()


def resolve_local_asset_file(root_dir: Path, raw_rel_path: str) -> Path:
    """
    将相对路径在 root_dir 下解析为真实文件并检查是否逃逸根目录（含符号链接防御）。
    """
    norm_rel = validate_relative_asset_path(raw_rel_path)
    root_resolved = root_dir.resolve(strict=True)
    candidate = (root_resolved / norm_rel).resolve(strict=True)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise PermissionError(f"资源路径逃逸出所选目录: {raw_rel_path}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(f"目标不是有效文件: {raw_rel_path}")
    return candidate


def list_root_markdown_files(target_path: str | Path) -> tuple[Path, list[Path]]:
    """若传文件则返回 (父目录, [文件])；若传目录则返回 (目录, 仅顶层 .md 列表)。"""
    p = Path(target_path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"找不到路径: {target_path}")
    if p.is_file():
        if p.suffix.lower() not in {".md", ".markdown"}:
            raise ValueError(f"所选文件不是 Markdown 文档: {p.name}")
        return p.parent.resolve(), [p.resolve()]
    if p.is_dir():
        root = p.resolve()
        md_files = sorted(
            child.resolve()
            for child in root.iterdir()
            if child.is_file() and child.suffix.lower() in {".md", ".markdown"}
        )
        return root, md_files
    raise ValueError(f"不支持的路径类型: {target_path}")


def scan_markdown_source(
    markdown_path: str | Path,
    *,
    root_dir: str | Path | None = None,
) -> ScannedMarkdownSource:
    """读取单篇 Markdown 并安全定位其引用的所有本地图片与附件。"""
    md_file = Path(markdown_path).expanduser().resolve(strict=True)
    root = Path(root_dir).expanduser().resolve(strict=True) if root_dir else md_file.parent
    try:
        md_file.relative_to(root)
    except ValueError as exc:
        raise PermissionError("Markdown 文件必须位于所选资料目录内。") from exc

    raw_text = md_file.read_text(encoding="utf-8-sig")
    fm, body, _ = parse_front_matter(raw_text)
    refs, ref_issues = extract_local_media_refs(body, fm)

    assets: list[LocalAssetFile] = []
    missing: list[str] = []
    blocked: list[str] = [iss.message for iss in ref_issues]

    for ref in refs:
        try:
            resolved_file = resolve_local_asset_file(root, ref.source_path)
            size_bytes, digest = _sha256_file(resolved_file)
            assets.append(
                LocalAssetFile(
                    asset_key=ref.asset_key,
                    source_path=ref.source_path,
                    media_type=ref.media_type,
                    local_path=resolved_file,
                    size_bytes=size_bytes,
                    sha256=digest,
                )
            )
        except FileNotFoundError:
            missing.append(ref.source_path)
        except (PermissionError, ValueError) as exc:
            blocked.append(str(exc))

    return ScannedMarkdownSource(
        root_dir=root,
        markdown_path=md_file,
        source_name=md_file.name,
        raw_text=raw_text,
        assets=tuple(assets),
        missing_assets=tuple(missing),
        blocked_paths=tuple(blocked),
    )
