/**
 * 受控第三方嵌入块前端安全渲染器 (embed-render.js)
 * 仅把白名单平台 URL 转换为严格 sandbox 限制的 16:9 iframe，其余降级为安全外链卡片。
 */

(function (window, document) {
    "use strict";

    function toSafeEmbedSrc(rawUrl) {
        var url;
        try {
            url = new URL(rawUrl);
        } catch (e) {
            return null;
        }
        if (url.protocol !== "http:" && url.protocol !== "https:") return null;
        var host = url.hostname.toLowerCase();

        // 1. Bilibili
        if (host === "player.bilibili.com") {
            url.protocol = "https:";
            url.searchParams.set("autoplay", "0");
            return url.toString();
        }
        if (host === "www.bilibili.com" || host === "bilibili.com") {
            var bvMatch = url.pathname.match(/\/video\/(BV[0-9A-Za-z]+)/);
            if (bvMatch) {
                return "https://player.bilibili.com/player.html?bvid=" + encodeURIComponent(bvMatch[1]) + "&high_quality=1&autoplay=0";
            }
        }

        // 2. YouTube
        if (host === "www.youtube.com" || host === "youtube.com") {
            if (url.pathname.indexOf("/embed/") === 0) {
                url.protocol = "https:";
                return url.toString();
            }
            var vid = url.searchParams.get("v");
            if (vid && /^[A-Za-z0-9_-]{6,20}$/.test(vid)) {
                return "https://www.youtube.com/embed/" + encodeURIComponent(vid);
            }
        }
        if (host === "youtu.be") {
            var shortId = url.pathname.replace(/^\/+/, "").split("/")[0];
            if (shortId && /^[A-Za-z0-9_-]{6,20}$/.test(shortId)) {
                return "https://www.youtube.com/embed/" + encodeURIComponent(shortId);
            }
        }

        // 3. Vimeo
        if (host === "player.vimeo.com" && url.pathname.indexOf("/video/") === 0) {
            url.protocol = "https:";
            return url.toString();
        }
        if (host === "vimeo.com" || host === "www.vimeo.com") {
            var vmMatch = url.pathname.match(/^\/([0-9]+)/);
            if (vmMatch) {
                return "https://player.vimeo.com/video/" + encodeURIComponent(vmMatch[1]);
            }
        }

        // 4. CodePen
        if (host === "codepen.io") {
            var cpMatch = url.pathname.match(/^\/([^/]+)\/(?:pen|embed)\/([A-Za-z0-9]+)/);
            if (cpMatch) {
                return "https://codepen.io/" + encodeURIComponent(cpMatch[1]) + "/embed/" + encodeURIComponent(cpMatch[2]) + "?default-tab=result";
            }
        }

        return null;
    }

    function initEmbedBlocks(root) {
        var container = root || document;
        container.querySelectorAll(".media-embed-wrapper[data-embed-url]").forEach(function (el) {
            if (el.hasAttribute("data-embed-rendered")) return;
            el.setAttribute("data-embed-rendered", "true");

            var rawUrl = el.getAttribute("data-embed-url") || "";
            var embedSrc = toSafeEmbedSrc(rawUrl);
            el.innerHTML = "";

            if (embedSrc) {
                var ratio = document.createElement("div");
                ratio.className = "cb-embed-ratio";
                var iframe = document.createElement("iframe");
                iframe.src = embedSrc;
                iframe.loading = "lazy";
                iframe.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
                iframe.setAttribute("sandbox", "allow-scripts allow-same-origin allow-presentation");
                iframe.setAttribute("allowfullscreen", "true");
                iframe.title = "第三方嵌入内容";
                ratio.appendChild(iframe);
                el.appendChild(ratio);
            } else {
                var fallback = document.createElement("div");
                fallback.className = "cb-embed-fallback";
                var label = document.createElement("span");
                label.textContent = "外部嵌入资源：";
                var link = document.createElement("a");
                link.href = rawUrl;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                link.textContent = rawUrl;
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
