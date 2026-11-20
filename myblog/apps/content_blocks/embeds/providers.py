"""
声明式内嵌平台注册表与统一安全解析引擎 (providers.py)。

核心原则：
1. 防 SSRF / 协议白名单：严格拦截非 http/https、localhost、内网私有 IP 与云元数据地址；
2. 严格 ID 提取：对每个支持平台只抽取受限字符集的媒体 ID 并按固定模板组装播放器 URL，
   不直接透传作者输入的任意查询参数；
3. 优雅降级：无法稳定提供官方内嵌播放器的平台（如酷狗、咪咕或未来通用链接卡片），
   降级为展示媒体标题与平台徽标的安全外链卡片，杜绝脆弱的负数定位页面裁剪 iframe；
4. 单一数据源：后端输出完整 data-embed-* 契约，前端 JS 不重复硬编码平台规则。
"""

from __future__ import annotations

import html
import ipaddress
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class EmbedProvider:
    key: str
    name: str
    kind: str  # "video" | "audio" | "rich" | "card"
    hosts: frozenset[str]
    example_url: str
    supports_iframe: bool
    resolver: Callable[[str, str], tuple[str | None, str | None]]


@dataclass(frozen=True)
class EmbedSpec:
    provider_key: str
    provider_name: str
    kind: str  # "video" | "audio" | "rich" | "card"
    url: str
    title: str
    embed_src: str | None
    is_iframe: bool


# Vimeo's player.js documentation publishes this exact sample and its hash.
# Recover it for the demo revision that predates support for unlisted URLs.
_VIMEO_DEMO_ID = "76979871"
_VIMEO_DEMO_HASH = "8272103f6e"
_TENCENT_LEGACY_DEMO_ID = "abc123456"


def _resolve_bilibili(url: str, _path: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "player.bilibili.com":
        qs = parse_qs(parsed.query)
        bvid = (qs.get("bvid") or [""])[0]
        if re.fullmatch(r"BV[0-9A-Za-z]{6,20}", bvid):
            return (
                f"https://player.bilibili.com/player.html?bvid={bvid}&high_quality=1&danmaku=0&autoplay=0",
                bvid,
            )
        return None, None
    m = re.search(r"/video/(BV[0-9A-Za-z]{6,20})", parsed.path)
    if m:
        bvid = m.group(1)
        return (
            f"https://player.bilibili.com/player.html?bvid={bvid}&high_quality=1&danmaku=0&autoplay=0",
            bvid,
        )
    return None, None


def _resolve_youtube(url: str, _path: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    vid = ""
    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path.startswith("/embed/"):
        vid = parsed.path[len("/embed/") :].split("/")[0]
    elif parsed.path.startswith("/shorts/"):
        vid = parsed.path[len("/shorts/") :].split("/")[0]
    else:
        vid = (parse_qs(parsed.query).get("v") or [""])[0]
    if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
        return f"https://www.youtube.com/embed/{vid}", vid
    return None, None


def _resolve_vimeo(url: str, _path: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    player = parsed.hostname == "player.vimeo.com"
    pattern = r"/video/([0-9]{4,15})/?" if player else r"/([0-9]{4,15})(?:/([0-9a-fA-F]{10}))?/?"
    match = re.fullmatch(pattern, parsed.path)
    if not match:
        return None, None
    vid = match[1]
    path_hash = "" if player else (match[2] or "")
    query_hashes = parse_qs(parsed.query, keep_blank_values=True).get("h", [])
    if query_hashes and (
        len(query_hashes) != 1
        or not re.fullmatch(r"[0-9a-fA-F]{10}", query_hashes[0])
        or (path_hash and path_hash != query_hashes[0])
    ):
        return None, None
    privacy_hash = path_hash or (query_hashes[0] if query_hashes else "")
    if vid == _VIMEO_DEMO_ID and not privacy_hash:
        privacy_hash = _VIMEO_DEMO_HASH
    suffix = f"?h={privacy_hash}" if privacy_hash else ""
    return f"https://player.vimeo.com/video/{vid}{suffix}", vid


def _resolve_codepen(url: str, _path: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    m = re.match(
        r"^/(?:(team/[A-Za-z0-9_-]{1,64}|[A-Za-z0-9_-]{1,64})/)?(?:pen|embed)/([A-Za-z0-9]{1,32})",
        parsed.path,
    )
    if m:
        user, slug = m.group(1), m.group(2)
        if user:
            return f"https://codepen.io/{user}/embed/{slug}?default-tab=result", slug
        return f"https://codepen.io/pen/embed/{slug}?default-tab=result", slug
    return None, None


def _resolve_tencent_video(url: str, _path: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    match = re.fullmatch(r"/x/(?:cover/[^/]+/|page/)([a-zA-Z0-9]{6,24})\.html", parsed.path)
    if match:
        vid = match[1]
    elif parsed.path == "/txp/iframe/player.html":
        vid = (parse_qs(parsed.query).get("vid") or [""])[0]
    else:
        return None, None
    if not re.fullmatch(r"[a-zA-Z0-9]{6,24}", vid):
        return None, None
    if vid == _TENCENT_LEGACY_DEMO_ID:
        return None, vid
    return f"https://v.qq.com/txp/iframe/player.html?vid={vid}", vid


def _resolve_youku(url: str, _path: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    m = re.search(r"/v_show/id_([a-zA-Z0-9=]{6,32})", parsed.path)
    if m:
        vid = m.group(1)
        return f"https://player.youku.com/embed/{vid}", vid
    m_emb = re.match(r"^/embed/([a-zA-Z0-9=]{6,32})", parsed.path)
    if m_emb:
        vid = m_emb.group(1)
        return f"https://player.youku.com/embed/{vid}", vid
    return None, None


def _resolve_netease_music(url: str, _path: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    song_id = (parse_qs(parsed.query).get("id") or [""])[0]
    if not song_id and parsed.fragment:
        frag_m = re.search(r"(?:song\?[^#]*?\bid=|outchain/\d+/)([0-9]{1,18})", parsed.fragment)
        if frag_m:
            song_id = frag_m.group(1)
    if re.fullmatch(r"[0-9]{1,18}", song_id):
        return (
            f"https://music.163.com/outchain/player?type=2&id={song_id}&auto=0&height=66",
            song_id,
        )
    return None, None


def _resolve_spotify(_url: str, path: str) -> tuple[str | None, str | None]:
    match = re.fullmatch(
        r"/(?:(?:embed|intl-[a-z]{2}(?:-[a-z]{2})?)/)?"
        r"(track|playlist|album|artist|episode|show)/([a-zA-Z0-9]{22})/?",
        path,
    )
    if not match:
        return None, None
    resource = f"{match[1]}/{match[2]}"
    return f"https://open.spotify.com/embed/{resource}", resource


def _resolve_kugou_card(url: str, _path: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    m = re.search(r"/mixsong/([a-zA-Z0-9]{6,32})\.html", parsed.path)
    if m:
        return None, m.group(1)
    if "/song/" in parsed.path:
        return None, "song"
    return None, None


def _resolve_migu_card(url: str, _path: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    m = re.search(r"/v3/music/song/([0-9]{4,24})", parsed.path)
    if m:
        return None, m.group(1)
    return None, None


EMBED_PROVIDERS: tuple[EmbedProvider, ...] = (
    EmbedProvider(
        key="bilibili",
        name="哔哩哔哩 (Bilibili)",
        kind="video",
        hosts=frozenset({"bilibili.com", "www.bilibili.com", "player.bilibili.com"}),
        example_url="https://www.bilibili.com/video/BV1xx411c7mD",
        supports_iframe=True,
        resolver=_resolve_bilibili,
    ),
    EmbedProvider(
        key="youtube",
        name="YouTube",
        kind="video",
        hosts=frozenset({"youtube.com", "www.youtube.com", "youtu.be"}),
        example_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        supports_iframe=True,
        resolver=_resolve_youtube,
    ),
    EmbedProvider(
        key="vimeo",
        name="Vimeo",
        kind="video",
        hosts=frozenset({"vimeo.com", "www.vimeo.com", "player.vimeo.com"}),
        example_url=f"https://vimeo.com/{_VIMEO_DEMO_ID}/{_VIMEO_DEMO_HASH}",
        supports_iframe=True,
        resolver=_resolve_vimeo,
    ),
    EmbedProvider(
        key="tencent_video",
        name="腾讯视频",
        kind="video",
        hosts=frozenset({"v.qq.com"}),
        example_url="https://v.qq.com/x/page/i0145yslf2c.html",
        supports_iframe=True,
        resolver=_resolve_tencent_video,
    ),
    EmbedProvider(
        key="youku",
        name="优酷视频",
        kind="video",
        hosts=frozenset({"v.youku.com", "player.youku.com"}),
        example_url="https://v.youku.com/v_show/id_XMTIzNDU2.html",
        supports_iframe=True,
        resolver=_resolve_youku,
    ),
    EmbedProvider(
        key="netease_music",
        name="网易云音乐",
        kind="audio",
        hosts=frozenset({"music.163.com"}),
        example_url="https://music.163.com/#/song?id=186016",
        supports_iframe=True,
        resolver=_resolve_netease_music,
    ),
    EmbedProvider(
        key="spotify",
        name="Spotify",
        kind="audio",
        hosts=frozenset({"open.spotify.com"}),
        example_url="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        supports_iframe=True,
        resolver=_resolve_spotify,
    ),
    EmbedProvider(
        key="kugou",
        name="酷狗音乐",
        kind="audio",
        hosts=frozenset({"www.kugou.com", "kugou.com"}),
        example_url="https://www.kugou.com/mixsong/abc123.html",
        supports_iframe=False,
        resolver=_resolve_kugou_card,
    ),
    EmbedProvider(
        key="migu",
        name="咪咕音乐",
        kind="audio",
        hosts=frozenset({"music.migu.cn"}),
        example_url="https://music.migu.cn/v3/music/song/60054701933",
        supports_iframe=False,
        resolver=_resolve_migu_card,
    ),
    EmbedProvider(
        key="codepen",
        name="CodePen",
        kind="rich",
        hosts=frozenset({"codepen.io"}),
        example_url="https://codepen.io/team/codepen/pen/PNaGbb",
        supports_iframe=True,
        resolver=_resolve_codepen,
    ),
)


def _validate_base_url(raw_url: str) -> tuple[bool, str, str]:
    url = (raw_url or "").strip()
    if not url:
        return False, "", "URL 不能为空"
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return False, "", "非法 URL 格式"

    if parsed.scheme not in ("http", "https"):
        return False, "", f"不支持的协议 {parsed.scheme}，仅允许 http/https"

    if parsed.username is not None or port not in (None, {"http": 80, "https": 443}[parsed.scheme]):
        return False, "", "媒体链接不能包含登录信息或非标准端口"
    if "\\" in url or any(char.isspace() or ord(char) < 32 for char in url):
        return False, "", "媒体链接包含无效字符"
    if not hostname:
        return False, "", "无法解析的主机名"

    if hostname in ("localhost", "127.0.0.1", "::1"):
        return False, "", "禁止嵌入本地回环地址 (SSRF 防御)"

    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, "", "禁止嵌入私有/内网 IP 地址 (SSRF 防御)"
    except ValueError:
        pass

    return True, hostname, ""


def resolve_embed_spec(raw_url: str, title: str = "") -> tuple[EmbedSpec | None, str]:
    """
    校验并解析外部流媒体/内嵌链接。
    返回 (EmbedSpec, "") 或 (None, error_reason)。
    """
    ok, hostname, err = _validate_base_url(raw_url)
    if not ok:
        return None, err

    clean_url = raw_url.strip()
    clean_title = (title or "").strip()
    for provider in EMBED_PROVIDERS:
        if hostname in provider.hosts:
            embed_src, resource_id = provider.resolver(clean_url, urlparse(clean_url).path)
            if provider.key == "vimeo" and embed_src:
                privacy_hash = parse_qs(urlparse(embed_src).query).get("h", [""])[0]
                suffix = f"/{privacy_hash}" if privacy_hash else ""
                clean_url = f"https://vimeo.com/{resource_id}{suffix}"
            elif provider.key == "spotify" and embed_src:
                # 原站回退链接指向内容页，不沿用播放器地址或分享追踪参数。
                clean_url = f"https://open.spotify.com/{resource_id}"
            if embed_src:
                return (
                    EmbedSpec(
                        provider_key=provider.key,
                        provider_name=provider.name,
                        kind=provider.kind,
                        url=clean_url,
                        title=clean_title or provider.name,
                        embed_src=embed_src,
                        is_iframe=True,
                    ),
                    "",
                )
            if resource_id or not provider.supports_iframe:
                return (
                    EmbedSpec(
                        provider_key=provider.key,
                        provider_name=provider.name,
                        kind=provider.kind,
                        url=clean_url,
                        title=clean_title or provider.name,
                        embed_src=None,
                        is_iframe=False,
                    ),
                    "",
                )
            return None, f"{provider.name} 链接格式无法识别，参考示例：{provider.example_url}"

    return None, f"域名 {hostname} 不在受信任的嵌入白名单中"


def is_safe_embed_url(url: str) -> tuple[bool, str]:
    """兼容原有校验入口：通过统一 EmbedProviderRegistry 判断 URL 是否安全受支持"""
    spec, err = resolve_embed_spec(url)
    return (spec is not None), err


def is_canonical_player_url(url: str) -> bool:
    """只允许注册表自己生成的 iframe 地址，不能直接信任作者提供的 data-*。"""
    spec, _ = resolve_embed_spec(url)
    return bool(spec and spec.embed_src == url)


def render_embed_wrapper_html(raw_url: str, title: str = "") -> str:
    """
    全站统一嵌入容器输出：供 CustomEmbedBlock 与 CustomRichTextBlock 共用。
    若 URL 未通过白名单/SSRF 校验，返回空字符串。
    """
    spec, _ = resolve_embed_spec(raw_url, title=title)
    if not spec:
        return ""

    if (
        spec.provider_key == "tencent_video"
        and not spec.embed_src
    ):
        return (
            '<div class="media-embed-wrapper">'
            f'<div class="cb-embed-header"><span class="cb-embed-title">{html.escape(spec.title)}</span></div>'
            '<div class="cb-embed-fallback">此演示使用了无效的占位链接，请在编辑器中替换为有效的腾讯视频链接。</div>'
            '</div>'
        )

    attrs: list[str] = [
        'class="media-embed-wrapper"',
        f'data-embed-url="{html.escape(spec.url, quote=True)}"',
        f'data-embed-provider="{html.escape(spec.provider_key, quote=True)}"',
        f'data-embed-provider-name="{html.escape(spec.provider_name, quote=True)}"',
        f'data-embed-kind="{html.escape(spec.kind, quote=True)}"',
        f'data-embed-title="{html.escape(spec.title, quote=True)}"',
    ]
    if spec.embed_src:
        attrs.append(f'data-embed-src="{html.escape(spec.embed_src, quote=True)}"')
    return f"<div {' '.join(attrs)}></div>"


def list_supported_providers() -> list[dict[str, str]]:
    """供管理后台展示支持的平台清单与示例链接"""
    return [
        {
            "key": p.key,
            "name": p.name,
            "kind": p.kind,
            "mode": "沙箱播放器" if p.supports_iframe else "安全媒体卡片",
            "example_url": p.example_url,
        }
        for p in EMBED_PROVIDERS
    ]
