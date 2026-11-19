/**
 * 全站前后台统一 Mermaid 11.17.2 安全渲染引擎 (mermaid-renderer.js)
 * 1. 仅按需加载锁定的 /static/vendor/mermaid/mermaid.min.js，不接纳页面上来源不明的 window.mermaid；
 * 2. 全局单队列串行执行 initialize -> render，彻底避免多图/多主题并发覆盖配置；
 * 3. 强制 securityLevel: "strict"、htmlLabels: false，并剔除恶意 %%{init:...}%% 指令；
 * 4. 严格 SVG 白名单净化（含根节点属性、事件、external href 与危险 CSS）。
 */

(function (window, document) {
    "use strict";

    var VENDOR_MERMAID_URL = "/static/vendor/mermaid/mermaid.min.js";
    var ALLOWED_THEMES = { default: true, dark: true, forest: true, neutral: true };
    var FORBIDDEN_TAGS = {
        script: true,
        foreignobject: true,
        iframe: true,
        object: true,
        embed: true,
        link: true,
        meta: true
    };

    var lockedMermaid = null;
    var loadPromise = null;
    var renderQueue = Promise.resolve();
    var seqCounter = 0;

    function normalizeTheme(theme) {
        var t = String(theme || "default").trim().toLowerCase();
        return ALLOWED_THEMES[t] ? t : "default";
    }

    function stripDangerousDirectives(code) {
        // 防止作者通过 %%{init: ...}%% 指令覆盖 securityLevel 或注入 htmlLabels/CSS
        return String(code || "").replace(/%%\{[\s\S]*?\}%%/g, "");
    }

    function loadLockedMermaid() {
        if (lockedMermaid) return Promise.resolve(lockedMermaid);
        if (loadPromise) return loadPromise;

        loadPromise = new Promise(function (resolve, reject) {
            var prevMermaid = window.mermaid;
            var script = document.createElement("script");
            script.src = VENDOR_MERMAID_URL;
            script.async = true;
            script.onload = function () {
                if (window.mermaid && typeof window.mermaid.render === "function") {
                    lockedMermaid = window.mermaid;
                    resolve(lockedMermaid);
                } else {
                    if (prevMermaid !== undefined) window.mermaid = prevMermaid;
                    reject(new Error("Locked Mermaid 11.17 runtime unavailable"));
                }
            };
            script.onerror = function () {
                loadPromise = null;
                reject(new Error("Mermaid script load failed"));
            };
            document.head.appendChild(script);
        });

        return loadPromise;
    }

    function cleanSvgNode(el) {
        var tagName = (el.tagName || "").toLowerCase();
        if (FORBIDDEN_TAGS[tagName]) {
            el.remove();
            return;
        }

        // 清理包括根节点 <svg> 在内的危险属性
        var attrs = Array.from(el.attributes || []);
        attrs.forEach(function (attr) {
            var name = attr.name.toLowerCase();
            var val = String(attr.value || "").trim();
            if (name.startsWith("on")) {
                el.removeAttribute(attr.name);
                return;
            }
            if (name === "href" || name === "xlink:href") {
                // 仅允许 SVG 内部 #fragment 引用，拒绝外链与 javascript:
                if (val && !val.startsWith("#")) {
                    el.removeAttribute(attr.name);
                }
                return;
            }
            if (name === "style" && /url\s*\(|expression\s*\(|@import/i.test(val)) {
                // 剔除包含外部 url() 的内联样式（保留无外链的内联字体/色彩）
                if (/url\s*\(\s*['"]?(?!#)/i.test(val) || /expression\s*\(|@import/i.test(val)) {
                    el.removeAttribute(attr.name);
                }
            }
        });

        if (tagName === "style") {
            var cssText = el.textContent || "";
            if (/expression\s*\(|@import|url\s*\(\s*['"]?(?!#)/i.test(cssText)) {
                el.textContent = cssText
                    .replace(/@import[^;]*;/gi, "")
                    .replace(/expression\s*\([^)]*\)/gi, "")
                    .replace(/url\s*\(\s*['"]?(?!#)[^)]*\)/gi, "none");
            }
        }

        Array.from(el.children || []).forEach(cleanSvgNode);
    }

    function sanitizeSvgString(rawSvg) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(String(rawSvg || ""), "image/svg+xml");
        if (doc.querySelector("parsererror")) return null;
        var svg = doc.querySelector("svg");
        if (!svg) return null;
        cleanSvgNode(svg);
        return svg;
    }

    function render(sourceCode, theme) {
        var cleanSource = stripDangerousDirectives(sourceCode).trim();
        var safeTheme = normalizeTheme(theme);
        if (!cleanSource) {
            return Promise.reject(new Error("图表源码为空"));
        }
        if (cleanSource.length > 50000) {
            return Promise.reject(new Error("图表源码超出 50000 字符安全限制"));
        }

        var task = renderQueue.then(function () {
            return loadLockedMermaid();
        }).then(function (mermaid) {
            mermaid.initialize({
                startOnLoad: false,
                securityLevel: "strict",
                theme: safeTheme,
                htmlLabels: false,
                fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans SC", "Microsoft YaHei", sans-serif'
            });
            var svgId = "cb_mmd_" + Date.now() + "_" + (++seqCounter);
            return mermaid.render(svgId, cleanSource);
        }).then(function (result) {
            var svgNode = sanitizeSvgString(result && result.svg);
            if (!svgNode) {
                throw new Error("SVG 图表净化失败");
            }
            return {
                svgNode: svgNode,
                svgHtml: svgNode.outerHTML,
                theme: safeTheme
            };
        });

        // 无论本次成功或失败，都保证队列继续畅通不卡死后续图表
        renderQueue = task.catch(function () {});
        return task;
    }

    window.CBMermaidRenderer = {
        normalizeTheme: normalizeTheme,
        render: render,
        sanitizeSvgString: sanitizeSvgString
    };
})(window, document);
