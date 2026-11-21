/**
 * 站内音视频公共播放器渐进增强层 (media-player.js)。
 *
 * 契约：
 * 1. 按需加载：页面没有站内音视频积木时不下载 vendor/videojs 资源；
 * 2. 同页互斥：任意站内音视频开始播放时暂停本页其它站内音视频；
 * 3. 安全降级：增强模块加载失败或未就绪时保持原生 controls；媒体本身异常显示明确错误提示；
 * 4. 前后台同源：导出 window.ContentBlocksMediaPlayers.{init, destroy} 供前台和 Wagtail 分栏复用。
 */

(function (window, document) {
    "use strict";

    var currentScript = document.currentScript ||
        document.querySelector("script[data-videojs-audio-url], script[data-videojs-video-url]");

    function resolveConfigUrl(attrName, fallback) {
        if (fallback) return fallback;
        if (currentScript && currentScript.getAttribute(attrName)) {
            return currentScript.getAttribute(attrName) || "";
        }
        var host = document.querySelector("[" + attrName + "]");
        return host ? host.getAttribute(attrName) || "" : "";
    }

    function getConfig() {
        return {
            audioUrl: resolveConfigUrl("data-videojs-audio-url", ""),
            videoUrl: resolveConfigUrl("data-videojs-video-url", ""),
            localeUrl: resolveConfigUrl("data-videojs-locale-url", ""),
            globalCssUrl: resolveConfigUrl("data-videojs-global-css-url", "")
        };
    }

    var globalCssReady = false;
    var localePromise = null;
    var modulePromises = { audio: null, video: null };
    var exclusiveListenerBound = false;
    var themeObserverBound = false;

    function getResolvedTheme() {
        var html = document.documentElement;
        if (html.getAttribute("data-theme") === "dark" || html.classList.contains("w-theme-dark")) {
            return "dark";
        }
        return "light";
    }

    function applyColorScheme(target) {
        if (!target) return;
        target.style.colorScheme = getResolvedTheme();
    }

    function ensureThemeSync() {
        if (themeObserverBound || typeof MutationObserver !== "function") return;
        themeObserverBound = true;
        var observer = new MutationObserver(function () {
            document.querySelectorAll(".cb-vjs-audio-player, .cb-vjs-video-player").forEach(applyColorScheme);
        });
        observer.observe(document.documentElement, {
            attributes: true,
            attributeFilter: ["data-theme", "class"]
        });
    }

    function bindExclusivePlayback() {
        if (exclusiveListenerBound) return;
        exclusiveListenerBound = true;
        document.addEventListener("play", function (event) {
            var active = event.target;
            if (!active || (active.tagName !== "AUDIO" && active.tagName !== "VIDEO")) return;
            if (!active.closest("[data-cb-media-kind]")) return;
            document.querySelectorAll("[data-cb-media-kind] audio, [data-cb-media-kind] video").forEach(function (other) {
                if (other !== active && !other.paused) {
                    try {
                        other.pause();
                    } catch (_err) {
                        // 忽略并发状态切换导致的暂停异常
                    }
                }
            });
        }, true);
    }

    function ensureGlobalCss() {
        var cssUrl = getConfig().globalCssUrl;
        if (globalCssReady || !cssUrl) return;
        if (document.querySelector('link[data-cb-videojs-global="true"]')) {
            globalCssReady = true;
            return;
        }
        var link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = cssUrl;
        link.setAttribute("data-cb-videojs-global", "true");
        document.head.appendChild(link);
        globalCssReady = true;
    }

    function ensureLocale() {
        var localeUrl = getConfig().localeUrl;
        if (!localeUrl) return Promise.resolve();
        if (!localePromise) {
            localePromise = import(localeUrl).catch(function () {
                // 语言包加载失败不影响播放器核心运行，保留默认文案
            });
        }
        return localePromise;
    }

    function loadKindModule(kind) {
        var cfg = getConfig();
        var url = kind === "audio" ? cfg.audioUrl : cfg.videoUrl;
        if (!url) return Promise.reject(new Error("Missing Video.js entry URL for " + kind));
        if (!modulePromises[kind]) {
            ensureGlobalCss();
            modulePromises[kind] = ensureLocale().then(function () {
                return import(url);
            }).catch(function (err) {
                modulePromises[kind] = null;
                throw err;
            });
        }
        return modulePromises[kind];
    }

    function showMediaError(wrapper, message) {
        if (!wrapper) return;
        var box = wrapper.querySelector(".cb-media-error");
        if (!box) {
            box = document.createElement("div");
            box.className = "cb-media-error";
            box.setAttribute("role", "alert");
            wrapper.appendChild(box);
        }
        box.textContent = message;
    }

    function clearMediaError(wrapper) {
        if (!wrapper) return;
        var box = wrapper.querySelector(".cb-media-error");
        if (box) box.remove();
    }

    function updateVideoAspectRatio(video, skin) {
        if (!video || !skin) return;
        if (video.videoWidth > 0 && video.videoHeight > 0) {
            skin.style.aspectRatio = video.videoWidth + " / " + video.videoHeight;
        }
    }

    function attachMediaStateListeners(wrapper, media, skin, kind) {
        if (!media || media.dataset.cbListenersBound === "true") return;
        media.dataset.cbListenersBound = "true";

        media.addEventListener("error", function () {
            showMediaError(
                wrapper,
                kind === "audio"
                    ? "音频暂时无法播放：文件可能缺失、无访问权限或格式不受当前浏览器支持。"
                    : "视频暂时无法播放：文件可能缺失、无访问权限或格式不受当前浏览器支持。"
            );
        });
        media.addEventListener("canplay", function () {
            clearMediaError(wrapper);
        });
        if (kind === "video") {
            media.addEventListener("loadedmetadata", function () {
                updateVideoAspectRatio(media, skin || media.closest("video-skin"));
            });
            if (media.readyState >= 1) {
                updateVideoAspectRatio(media, skin || media.closest("video-skin"));
            }
        }
    }

    function waitForSkinControls(skinTag, skinEl, timeoutMs) {
        if (!window.customElements) {
            return Promise.reject(new Error("CustomElements unavailable"));
        }
        return new Promise(function (resolve, reject) {
            var frame;
            var timer = window.setTimeout(function () {
                window.cancelAnimationFrame(frame);
                reject(new Error("Skin upgrade timed out"));
            }, timeoutMs || 6000);

            // 注册自定义元素不等于其内部控件已渲染，不能只等一帧。
            function check() {
                if (window.customElements.get(skinTag) &&
                    skinEl.shadowRoot && skinEl.shadowRoot.querySelector("media-play-button")) {
                    window.clearTimeout(timer);
                    resolve();
                } else {
                    frame = window.requestAnimationFrame(check);
                }
            }
            check();
        });
    }

    function upgradeOneWrapper(wrapper) {
        var kind = wrapper.getAttribute("data-cb-media-kind");
        if (kind !== "audio" && kind !== "video") return;
        if (wrapper.getAttribute("data-cb-media-init") === "pending" ||
            wrapper.getAttribute("data-cb-media-init") === "ready") {
            return;
        }

        var media = wrapper.querySelector(kind);
        if (!media) return;

        wrapper.setAttribute("data-cb-media-init", "pending");
        clearMediaError(wrapper);

        var playerTag = kind === "audio" ? "audio-player" : "video-player";
        var skinTag = kind === "audio" ? "audio-skin" : "video-skin";
        var existingSkin = media.closest(skinTag);

        attachMediaStateListeners(wrapper, media, existingSkin, kind);

        loadKindModule(kind).then(function () {
            if (!wrapper.isConnected || !media.isConnected) return;

            var playerEl = media.closest(playerTag);
            var skinEl = media.closest(skinTag);
            if (!playerEl || !skinEl) {
                playerEl = document.createElement(playerTag);
                playerEl.className = kind === "audio" ? "cb-vjs-audio-player" : "cb-vjs-video-player";
                skinEl = document.createElement(skinTag);
                skinEl.className = kind === "audio" ? "cb-vjs-audio-skin" : "cb-vjs-video-skin";
                applyColorScheme(playerEl);
                var stage = wrapper.querySelector(".cb-media-stage") || media.parentNode;
                stage.insertBefore(playerEl, media);
                skinEl.appendChild(media);
                playerEl.appendChild(skinEl);
                if (kind === "video") {
                    updateVideoAspectRatio(media, skinEl);
                }
            }

            return waitForSkinControls(skinTag, skinEl, 6000).then(function () {
                if (!wrapper.isConnected || !media.isConnected) return;
                media.controls = false;
                media.removeAttribute("controls");
                wrapper.setAttribute("data-cb-media-init", "ready");
            });
        }).catch(function () {
            if (!media || !media.isConnected) return;
            // 增强失败时确保还原浏览器原生控件
            var playerEl = media.closest(playerTag);
            if (playerEl && playerEl.parentNode) {
                playerEl.parentNode.insertBefore(media, playerEl);
                playerEl.remove();
            }
            media.controls = true;
            media.setAttribute("controls", "");
            wrapper.setAttribute("data-cb-media-init", "native-fallback");
        });
    }

    function initMediaPlayers(root) {
        var container = root || document;
        var targets = [];
        if (container.nodeType === 1 && container.hasAttribute && container.hasAttribute("data-cb-media-kind")) {
            targets.push(container);
        }
        container.querySelectorAll("[data-cb-media-kind]").forEach(function (el) {
            targets.push(el);
        });
        if (!targets.length) return;

        bindExclusivePlayback();
        ensureThemeSync();
        targets.forEach(upgradeOneWrapper);
    }

    function destroyMediaPlayers(root) {
        var container = root || document;
        var targets = [];
        if (container.nodeType === 1 && container.hasAttribute && container.hasAttribute("data-cb-media-kind")) {
            targets.push(container);
        }
        container.querySelectorAll("[data-cb-media-kind]").forEach(function (el) {
            targets.push(el);
        });
        targets.forEach(function (wrapper) {
            wrapper.querySelectorAll("audio, video").forEach(function (media) {
                try {
                    media.pause();
                } catch (_err) {}
                media.removeAttribute("src");
                try {
                    media.load();
                } catch (_err2) {}
            });
            wrapper.removeAttribute("data-cb-media-init");
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            initMediaPlayers(document);
        });
    } else {
        initMediaPlayers(document);
    }

    window.ContentBlocksMediaPlayers = {
        init: initMediaPlayers,
        destroy: destroyMediaPlayers
    };
})(window, document);
