/**
 * 受控第三方嵌入块前端安全渲染器 (embed-render.js)
 * 完全由后端 EmbedProviderRegistry 输出的 data-embed-* 契约驱动：
 * - 视频/交互类 (video/rich): 16:9 沙箱 iframe + 真实媒体标题栏；
 * - 音频播放器类 (audio with embed_src): 紧凑横条播放器 + 真实媒体标题栏；
 * - 媒体卡片类 (无 embed_src，如酷狗/咪咕/未来通用链接): 安全外部媒体卡片。
 */

(function (window, document) {
    "use strict";

    function webUrl(raw) {
        try {
            var url = new URL(raw);
            if (["http:", "https:"].includes(url.protocol) && !url.username && !url.password) {
                return url;
            }
        } catch (_) {
            // 无效或缺失的地址不生成链接。
        }
        return null;
    }

    function displayTitle(title, provider) {
        var colon = title.search(/[:：]/);
        if (colon > 0) {
            var normalize = function (text) { return text.replace(/\s+/g, "").toLowerCase(); };
            var prefix = normalize(title.slice(0, colon));
            if (prefix === normalize(provider) || prefix === normalize(provider.split(" (")[0])) {
                return title.slice(colon + 1).trim() || title;
            }
        }
        return title;
    }

    function mountVimeoPlayer(el, box, iframe, src) {
        var status = document.createElement("div");
        status.className = "cb-embed-fallback";
        var text = document.createElement("span");
        text.setAttribute("role", "status");
        var play = document.createElement("button");
        play.type = "button";
        play.textContent = "加载 Vimeo 播放器";
        status.append(play, text);
        el.appendChild(status);

        play.addEventListener("click", function () {
            play.disabled = true;
            text.textContent = "正在连接 Vimeo…";
            // HTTP load 也可能是第三方错误页；只在官方播放器应答后显示画布。
            box.hidden = true;
            iframe.loading = "eager";
            var timer = window.setTimeout(fallback, 8000);

            function cleanup() {
                window.clearTimeout(timer);
                window.removeEventListener("message", onMessage);
                iframe.removeEventListener("load", ping);
                iframe.removeEventListener("error", fallback);
            }
            function fallback() {
                cleanup();
                box.remove();
                play.remove();
                text.textContent = "暂时无法在站内播放，请通过上方源链接观看。";
            }
            function ping() {
                iframe.contentWindow.postMessage({ method: "ping" }, src.origin);
            }
            function onMessage(event) {
                if (event.origin !== src.origin || event.source !== iframe.contentWindow) return;
                var data = event.data;
                if (typeof data === "string") {
                    try { data = JSON.parse(data); } catch (_) { return; }
                }
                if (!data) return;
                if (data.event === "error" && data.data && data.data.method === "ready") {
                    fallback();
                } else if (data.event === "ready" || data.method === "ping") {
                    cleanup();
                    status.remove();
                    box.hidden = false;
                }
            }

            window.addEventListener("message", onMessage);
            iframe.addEventListener("load", ping);
            iframe.addEventListener("error", fallback);
            iframe.src = src.href;
            el.appendChild(box);
        }, { once: true });
    }

    function initEmbedBlocks(root) {
        var container = root || document;
        container.querySelectorAll(".media-embed-wrapper[data-embed-url]").forEach(function (el) {
            if (el.hasAttribute("data-embed-rendered")) return;
            el.setAttribute("data-embed-rendered", "true");

            var source = webUrl(el.getAttribute("data-embed-url"));
            if (!source) return;
            var embedSrc = webUrl(el.getAttribute("data-embed-src"));
            var mediaTitle = el.getAttribute("data-embed-title") || "第三方嵌入内容";
            var providerName = el.getAttribute("data-embed-provider-name") || "外部媒体";
            var kind = el.getAttribute("data-embed-kind") || "video";

            el.innerHTML = "";

            var header = document.createElement("div");
            header.className = "cb-embed-header";
            var badge = document.createElement("span");
            badge.className = "cb-embed-provider";
            badge.textContent = providerName;
            var titleEl = document.createElement("span");
            titleEl.className = "cb-embed-title";
            titleEl.textContent = displayTitle(mediaTitle, providerName);
            var extLink = document.createElement("a");
            extLink.className = "cb-embed-ext-link";
            extLink.href = source.href;
            extLink.target = "_blank";
            extLink.rel = "noopener noreferrer";
            extLink.textContent = "源链接 ↗";
            header.append(badge, titleEl, extLink);
            el.appendChild(header);

            if (embedSrc && embedSrc.protocol === "https:" && embedSrc.origin !== window.location.origin) {
                var box = document.createElement("div");
                box.className = kind === "audio" ? "cb-embed-audio-box" : "cb-embed-ratio";
                var iframe = document.createElement("iframe");
                iframe.loading = "lazy";
                iframe.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
                iframe.setAttribute("sandbox", "allow-scripts allow-same-origin allow-presentation");
                iframe.setAttribute("allowfullscreen", "true");
                if (embedSrc.origin === "https://open.spotify.com") {
                    iframe.setAttribute("allow", "encrypted-media");
                }
                iframe.title = titleEl.textContent;
                box.appendChild(iframe);
                if (embedSrc.origin === "https://player.vimeo.com") {
                    mountVimeoPlayer(el, box, iframe, embedSrc);
                } else {
                    iframe.src = embedSrc.href;
                    el.appendChild(box);
                }
            } else {
                var fallback = document.createElement("div");
                fallback.className = "cb-embed-fallback";
                var label = document.createElement("span");
                label.textContent = kind === "audio" ? "在原平台收听" : "在原平台查看";
                var link = document.createElement("a");
                link.href = source.href;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                link.textContent = "前往 " + providerName + " 收听 / 查看 ↗";
                fallback.append(label, link);
                el.appendChild(fallback);
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            initEmbedBlocks(document);
        });
    } else {
        initEmbedBlocks(document);
    }

    window.initEmbedBlocks = initEmbedBlocks;
})(window, document);
