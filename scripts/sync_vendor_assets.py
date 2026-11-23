#!/usr/bin/env python3
"""
前端公共第三方库（vendor）锁定同步、校验与升级脚本。

使用纯 Python 标准库实现，不依赖 Node.js / npm 运行时，
用于在全新环境、CI 或 Docker 构建阶段按 myblog/static/vendor.lock.json
还原并校验 myblog/static/vendor/ 下的运行时静态文件。

常用命令：
  python scripts/sync_vendor_assets.py check
  python scripts/sync_vendor_assets.py sync
  python scripts/sync_vendor_assets.py sync --cache-dir /tmp/pkg-inspect --force
  python scripts/sync_vendor_assets.py upgrade mermaid 11.17.2 --dry-run
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK_FILE = REPO_ROOT / "myblog" / "static" / "vendor.lock.json"
MANIFEST_NAME = ".vendor-manifest.json"
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z.-]+)?$")
ALLOWED_REGISTRY_PREFIX = "https://registry.npmjs.org/"


def load_lock(lock_path: Path) -> dict[str, Any]:
    if not lock_path.is_file():
        raise FileNotFoundError(f"找不到 vendor 锁文件: {lock_path}")
    return json.loads(lock_path.read_text(encoding="utf-8"))


def verify_archive_checksums(
    archive_path: Path, expected_sha512_b64: str | None, expected_sha1: str | None
) -> None:
    h512 = hashlib.sha512()
    h1 = hashlib.sha1()
    with archive_path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h512.update(chunk)
            h1.update(chunk)
    actual_sha512_b64 = base64.b64encode(h512.digest()).decode("ascii")
    actual_sha1 = h1.hexdigest()

    if expected_sha512_b64 and actual_sha512_b64 != expected_sha512_b64:
        raise ValueError(
            f"SHA-512 校验失败: {archive_path.name} "
            f"(expected={expected_sha512_b64}, actual={actual_sha512_b64})"
        )
    if expected_sha1 and actual_sha1 != expected_sha1:
        raise ValueError(
            f"SHA-1 校验失败: {archive_path.name} "
            f"(expected={expected_sha1}, actual={actual_sha1})"
        )


def download_tarball(url: str, dest: Path) -> None:
    if not url.startswith(ALLOWED_REGISTRY_PREFIX):
        raise ValueError(f"拒绝非官方 npm registry 地址: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "myblog-vendor-sync/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)


def safe_extract_tar(archive_path: Path, extract_root: Path) -> None:
    extract_root_resolved = extract_root.resolve()
    with tarfile.open(archive_path, "r:gz") as tf:
        for member in tf.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"压缩包内存在不允许的链接条目: {member.name}")
            member_path = (extract_root / member.name).resolve()
            if member_path != extract_root_resolved and extract_root_resolved not in member_path.parents:
                raise ValueError(f"检测到路径穿越风险条目: {member.name}")
        tf.extractall(extract_root, filter="data")


def build_upstream_md(pkg_name: str, spec: dict[str, Any]) -> str:
    lines = [
        f"# {pkg_name} Upstream",
        "",
        f"- Package: {pkg_name}",
        f"- Version: {spec['version']}",
        f"- Upstream: {spec.get('upstream', '')}",
        f"- License: {spec.get('license', '')}",
        f"- Tarball: {spec.get('registry_url', '')}",
        f"- Integrity (sha512): {spec.get('integrity_sha512', '')}",
    ]
    if spec.get("security_note"):
        lines.append(f"- Security Note: {spec['security_note']}")
    return "\n".join(lines) + "\n"


def _install_rules_digest(spec: dict[str, Any]) -> str:
    rules = {
        key: spec.get(key)
        for key in ("extract", "strip_missing_sourcemaps", "fix_vditor_markmap_katex_fonts")
    }
    return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()


def package_is_current(pkg_dir: Path, spec: dict[str, Any]) -> tuple[bool, str]:
    manifest_path = pkg_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return False, "缺少 .vendor-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"manifest 解析失败: {exc}"

    if manifest.get("version") != spec["version"]:
        return False, f"版本不一致 (local={manifest.get('version')}, lock={spec['version']})"
    if manifest.get("integrity_sha512") != spec.get("integrity_sha512"):
        return False, "SHA-512 摘要与锁文件不一致"
    digest = manifest.get("install_rules_sha256")
    if digest is not None or spec.get("strip_missing_sourcemaps") or spec.get("fix_vditor_markmap_katex_fonts"):
        if digest != _install_rules_digest(spec):
            return False, "提取或兼容规则已变化"

    for rule in spec.get("extract", []):
        target = pkg_dir / rule["dest"]
        if rule.get("recursive"):
            if not target.is_dir() or not any(target.iterdir()):
                return False, f"目录缺失或为空: {rule['dest']}"
        else:
            if not target.is_file() or target.stat().st_size == 0:
                return False, f"文件缺失或为空: {rule['dest']}"

    return True, "OK"


_SOURCEMAP_LINE_RE = re.compile(
    rb"(?m)^[ \t]*//[#@][ \t]*sourceMappingURL=([A-Za-z0-9._/\-]+\.map)[ \t]*\r?$(?:\n)?"
)
_CSS_SOURCEMAP_RE = re.compile(
    rb"[ \t]*/\*[#@][ \t]*sourceMappingURL=([A-Za-z0-9._/\-]+\.map)[ \t]*\*/[ \t]*(?:\r?\n)?"
)


def _strip_missing_sourcemaps(pkg_root: Path) -> int:
    """移除 JS/CSS 中指向缺失 .map 文件的 sourceMappingURL 注释，避免 ManifestStaticFilesStorage 报错。"""
    removed = 0
    for pattern, regex in (("*.js", _SOURCEMAP_LINE_RE), ("*.css", _CSS_SOURCEMAP_RE)):
        for asset_path in pkg_root.rglob(pattern):
            if not asset_path.is_file():
                continue
            raw = asset_path.read_bytes()
            if b"sourceMappingURL=" not in raw:
                continue

            def _replace(match: re.Match[bytes], *, current_path: Path = asset_path) -> bytes:
                nonlocal removed
                rel_map = match.group(1).decode("ascii", errors="ignore")
                if (current_path.parent / rel_map).is_file():
                    return match.group(0)
                removed += 1
                return b""

            updated = regex.sub(_replace, raw)
            if updated != raw:
                asset_path.write_bytes(updated)
    return removed


def _fix_vditor_markmap_katex_fonts(pkg_root: Path) -> bool:
    """修正 Vditor markmap/katex.min.css 中指向缺失相对字体目录的路径为同包已有 ../katex/fonts/。"""
    css_path = pkg_root / "dist" / "js" / "markmap" / "katex.min.css"
    katex_fonts_dir = pkg_root / "dist" / "js" / "katex" / "fonts"
    if not css_path.is_file() or not katex_fonts_dir.is_dir():
        return False
    if (css_path.parent / "fonts").is_dir():
        return False
    raw = css_path.read_bytes()
    if b"url(fonts/" not in raw:
        return False
    referenced = re.findall(rb"url\(fonts/([A-Za-z0-9._-]+)\)", raw)
    if not referenced:
        return False
    if not all((katex_fonts_dir / name.decode("ascii", errors="ignore")).is_file() for name in referenced):
        return False
    css_path.write_bytes(raw.replace(b"url(fonts/", b"url(../katex/fonts/"))
    return True


def cmd_check(lock_path: Path) -> int:
    lock = load_lock(lock_path)
    target_root = REPO_ROOT / lock["target_root"]
    all_ok = True
    for pkg_name, spec in lock["packages"].items():
        pkg_dir = target_root / pkg_name
        ok, reason = package_is_current(pkg_dir, spec)
        status = "OK" if ok else f"FAIL ({reason})"
        print(f"[check] {pkg_name}@{spec['version']}: {status}")
        if not ok:
            all_ok = False
    return 0 if all_ok else 1


def sync_one_package(
    pkg_name: str,
    spec: dict[str, Any],
    target_root: Path,
    *,
    force: bool,
    dry_run: bool,
    cache_dir: Path | None,
) -> None:
    pkg_dir = target_root / pkg_name
    if not force:
        ok, _ = package_is_current(pkg_dir, spec)
        if ok:
            print(f"[sync] {pkg_name}@{spec['version']} 已是最新且校验通过，跳过。")
            return

    if dry_run:
        print(f"[dry-run] 将同步 {pkg_name}@{spec['version']} -> {pkg_dir}")
        return

    target_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"vendor-{pkg_name}-") as tmp:
        tmp_path = Path(tmp)
        archive_name = f"{pkg_name}-{spec['version']}.tgz"
        npm_pkg = spec.get("npm_package", pkg_name)
        candidate_names = [
            archive_name,
            Path(spec["registry_url"]).name,
            f"{npm_pkg.lstrip('@').replace('/', '-')}-{spec['version']}.tgz",
        ]
        cached = None
        if cache_dir:
            for cname in candidate_names:
                p = cache_dir / cname
                if p.is_file():
                    cached = p
                    break
        archive_path = tmp_path / archive_name

        if cached and cached.is_file():
            print(f"[sync] 使用本地缓存包: {cached}")
            shutil.copy2(cached, archive_path)
        else:
            print(f"[sync] 下载 {spec['registry_url']} ...")
            download_tarball(spec["registry_url"], archive_path)

        verify_archive_checksums(
            archive_path,
            spec.get("integrity_sha512"),
            spec.get("shasum_sha1"),
        )

        extracted_dir = tmp_path / "extracted"
        extracted_dir.mkdir()
        safe_extract_tar(archive_path, extracted_dir)

        staged_pkg_dir = tmp_path / "staged"
        staged_pkg_dir.mkdir()

        for rule in spec.get("extract", []):
            src_path = extracted_dir / rule["src"]
            dest_path = (staged_pkg_dir / rule["dest"]).resolve()
            staged_root_resolved = staged_pkg_dir.resolve()
            if dest_path != staged_root_resolved and staged_root_resolved not in dest_path.parents:
                raise ValueError(f"非法提取目标路径: {rule['dest']}")
            if rule.get("recursive"):
                if not src_path.is_dir():
                    raise FileNotFoundError(f"{pkg_name} 压缩包缺少目录: {rule['src']}")
                exclude_suffixes = tuple(rule.get("exclude_suffixes", []))
                ignore_fn = None
                if exclude_suffixes:
                    def _ignore(directory: str, names: list[str]) -> set[str]:
                        dir_path = Path(directory)
                        return {
                            name
                            for name in names
                            if (dir_path / name).is_file() and name.endswith(exclude_suffixes)
                        }
                    ignore_fn = _ignore
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True, ignore=ignore_fn)
            else:
                if not src_path.is_file():
                    raise FileNotFoundError(f"{pkg_name} 压缩包缺少文件: {rule['src']}")
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)

        if spec.get("strip_missing_sourcemaps"):
            _strip_missing_sourcemaps(staged_pkg_dir)
        if spec.get("fix_vditor_markmap_katex_fonts"):
            _fix_vditor_markmap_katex_fonts(staged_pkg_dir)

        (staged_pkg_dir / "UPSTREAM.md").write_text(
            build_upstream_md(pkg_name, spec), encoding="utf-8"
        )
        manifest_payload = {
            "package": pkg_name,
            "version": spec["version"],
            "integrity_sha512": spec.get("integrity_sha512"),
            "shasum_sha1": spec.get("shasum_sha1"),
            "license": spec.get("license"),
            "install_rules_sha256": _install_rules_digest(spec),
        }
        (staged_pkg_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # 确保最终路径仍在 target_root 下，再替换目标子目录
        pkg_dir_resolved = pkg_dir.resolve()
        target_root_resolved = target_root.resolve()
        if target_root_resolved not in pkg_dir_resolved.parents:
            raise ValueError(f"非法目标路径: {pkg_dir_resolved}")

        if pkg_dir.exists():
            shutil.rmtree(pkg_dir)
        shutil.copytree(staged_pkg_dir, pkg_dir)
        print(f"[sync] 已同步 {pkg_name}@{spec['version']} 到 {pkg_dir}")


def cmd_sync(
    lock_path: Path,
    *,
    force: bool,
    dry_run: bool,
    cache_dir: Path | None,
) -> int:
    lock = load_lock(lock_path)
    target_root = REPO_ROOT / lock["target_root"]
    for pkg_name, spec in lock["packages"].items():
        sync_one_package(
            pkg_name,
            spec,
            target_root,
            force=force,
            dry_run=dry_run,
            cache_dir=cache_dir,
        )
    return 0


def cmd_upgrade(
    lock_path: Path,
    package: str,
    version: str,
    *,
    dry_run: bool,
    run_sync: bool,
) -> int:
    if not VERSION_RE.match(version):
        raise ValueError(f"版本号格式不合法: {version}")
    lock = load_lock(lock_path)
    packages = lock.get("packages", {})
    if package not in packages:
        raise KeyError(f"vendor.lock.json 未登记包 '{package}'，当前支持: {list(packages.keys())}")

    npm_pkg = packages[package].get("npm_package", package)
    meta_url = f"{ALLOWED_REGISTRY_PREFIX}{npm_pkg}/{version}"
    req = urllib.request.Request(meta_url, headers={"User-Agent": "myblog-vendor-sync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        meta = json.loads(resp.read().decode("utf-8"))

    dist = meta.get("dist", {})
    tarball = dist.get("tarball", "")
    integrity = dist.get("integrity", "")
    shasum = dist.get("shasum", "")
    if not tarball.startswith(ALLOWED_REGISTRY_PREFIX) or not integrity.startswith("sha512-"):
        raise ValueError(f"npm 元数据缺少有效的 tarball 或 sha512 校验信息: {meta_url}")

    sha512_b64 = integrity.removeprefix("sha512-")
    old_version = packages[package]["version"]
    print(f"[upgrade] {package}: {old_version} -> {version}")
    print(f"  tarball: {tarball}")
    print(f"  sha512 : {sha512_b64}")
    print(f"  shasum : {shasum}")

    if dry_run:
        print("[dry-run] 未写入 vendor.lock.json。")
        return 0

    packages[package]["version"] = version
    packages[package]["registry_url"] = tarball
    packages[package]["integrity_sha512"] = sha512_b64
    packages[package]["shasum_sha1"] = shasum
    packages[package]["security_note"] = f"Updated via sync_vendor_assets.py from {old_version} to {version}"

    lock_path.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[upgrade] 已更新 {lock_path}")

    if run_sync:
        return cmd_sync(lock_path, force=True, dry_run=False, cache_dir=None)
    print("提示：请运行 `python scripts/sync_vendor_assets.py sync` 更新本地静态文件并执行完整回归测试。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理 myblog/static/vendor 第三方前端库")
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE, help="指定 vendor.lock.json 路径")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("check", help="校验本地 vendor 目录是否与 vendor.lock.json 一致")

    sync_p = subparsers.add_parser("sync", help="按 vendor.lock.json 下载/恢复并校验第三方静态文件")
    sync_p.add_argument("--force", action="store_true", help="即使版本一致也强制重新提取")
    sync_p.add_argument("--dry-run", action="store_true", help="仅预览将同步的目录，不写入文件")
    sync_p.add_argument("--cache-dir", type=Path, default=None, help="优先从指定本地目录读取 .tgz 离线包")

    up_p = subparsers.add_parser("upgrade", help="从 npm 查询并更新 vendor.lock.json 中的指定包版本")
    up_p.add_argument("package", help="包名称（如 mermaid 或 katex）")
    up_p.add_argument("version", help="目标精确版本号（如 11.17.2）")
    up_p.add_argument("--dry-run", action="store_true", help="仅查询对比版本，不改写锁文件")
    up_p.add_argument("--sync", action="store_true", help="更新锁文件后立即同步到本地 vendor 目录")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.command or "sync"

    if cmd == "check":
        return cmd_check(args.lock_file)
    if cmd == "sync":
        return cmd_sync(
            args.lock_file,
            force=args.force,
            dry_run=args.dry_run,
            cache_dir=args.cache_dir,
        )
    if cmd == "upgrade":
        return cmd_upgrade(
            args.lock_file,
            args.package,
            args.version,
            dry_run=args.dry_run,
            run_sync=args.sync,
        )
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
