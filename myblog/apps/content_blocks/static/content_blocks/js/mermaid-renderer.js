/**
 * 全站前后台统一 Mermaid 11.17.2 安全渲染引擎 (mermaid-renderer.js)
 * 1. 仅按需加载锁定的 /static/vendor/mermaid/mermaid.min.js，不接纳页面上来源不明的 window.mermaid；
 * 2. 全局单队列串行执行 initialize -> render，彻底避免多图/多主题并发覆盖配置；
 * 3. 先在原文上强制执行 50,000 字符上限，再剥离开头 YAML frontmatter 与 %%{init:...}%% 指令（保留等量换行以保真行号），
 *    强制 securityLevel: "strict"、htmlLabels: false；
 * 4. 严格 SVG 白名单净化（含根节点属性、事件、external href 与危险 CSS），并提供离线安全导出辅助方法。
 */

(function (window, document) {
    "use strict";

    var MAX_SOURCE_CHARS = 50000;
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
        // 规范换行并剔除开头 YAML frontmatter 与 %%{init: ...}%%，用等量换行占位以保持原文行号映射准确
        var normalized = String(code || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
        normalized = normalized.replace(/^[ \t]*---\n[\s\S]*?\n---[ \t]*(?:\n|$)/, function (m) {
            return m.replace(/[^\n]/g, "");
        });
        normalized = normalized.replace(/%%\{[\s\S]*?\}%%/g, function (m) {
            return m.replace(/[^\n]/g, "");
        });
        return normalized;
    }

    function extractDiagnosticError(err, rawNormalized, leadingOffsetLines) {
        var rawMsg = (err && (err.str || err.message)) ? String(err.str || err.message) : "请检查 Mermaid 语法";
        var lines = rawNormalized.split("\n");
        var maxLine = lines.length;
        var parsedLine = null;

        if (err && err.hash && err.hash.loc && typeof err.hash.loc.first_line === "number") {
            parsedLine = err.hash.loc.first_line;
        } else {
            var mLine = rawMsg.match(/(?:Parse error on line|on line|line)\s+(\d+)/i);
            if (mLine) {
                parsedLine = parseInt(mLine[1], 10);
            }
        }
        if (parsedLine && leadingOffsetLines > 0) {
            parsedLine += leadingOffsetLines;
        }
        if (!parsedLine || parsedLine < 1 || parsedLine > maxLine) {
            parsedLine = null;
        }

        var summary = rawMsg.split("\n")[0].trim();
        if (summary.length > 220) {
            summary = summary.slice(0, 217) + "...";
        }
        var outErr = new Error(summary || "Mermaid 语法解析失败");
        outErr.cbLine = parsedLine;
        outErr.cbSummary = summary || "Mermaid 语法解析失败";
        return outErr;
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

    function cloneSanitizedSvg(svgElement) {
        if (!svgElement || (svgElement.tagName || "").toLowerCase() !== "svg") return null;
        var clone = svgElement.cloneNode(true);
        cleanSvgNode(clone);
        if (!clone.getAttribute("xmlns")) {
            clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
        }
        if (!clone.getAttribute("xmlns:xlink")) {
            clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
        }
        return clone;
    }

    function serializeSafeSvg(svgElement) {
        var cleanClone = cloneSanitizedSvg(svgElement);
        if (!cleanClone) return "";
        var serializer = new XMLSerializer();
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + serializer.serializeToString(cleanClone);
    }

    function render(sourceCode, theme) {
        var rawStr = String(sourceCode || "");
        if (rawStr.length > MAX_SOURCE_CHARS) {
            var limitErr = new Error("图表源码超出 " + MAX_SOURCE_CHARS + " 字符安全限制（当前 " + rawStr.length + " 字符）");
            limitErr.cbLine = null;
            limitErr.cbSummary = limitErr.message;
            return Promise.reject(limitErr);
        }

        var rawNormalized = rawStr.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
        var cleanSource = stripDangerousDirectives(rawNormalized);
        var leadingWs = (cleanSource.match(/^\s*/) || [""])[0];
        var leadingOffsetLines = leadingWs ? (leadingWs.split("\n").length - 1) : 0;
        var trimmedForRender = cleanSource.trim();
        var safeTheme = normalizeTheme(theme);
        if (!trimmedForRender) {
            var emptyErr = new Error("图表源码为空");
            emptyErr.cbLine = null;
            emptyErr.cbSummary = emptyErr.message;
            return Promise.reject(emptyErr);
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
            return mermaid.render(svgId, trimmedForRender);
        }, function (loadErr) {
            throw extractDiagnosticError(loadErr, rawNormalized, leadingOffsetLines);
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
        }, function (renderErr) {
            throw extractDiagnosticError(renderErr, rawNormalized, leadingOffsetLines);
        });

        // 无论本次成功或失败，都保证队列继续畅通不卡死后续图表
        renderQueue = task.catch(function () {});
        return task;
    }

    window.CBMermaidRenderer = {
        MAX_SOURCE_CHARS: MAX_SOURCE_CHARS,
        normalizeTheme: normalizeTheme,
        render: render,
        sanitizeSvgString: sanitizeSvgString,
        cloneSanitizedSvg: cloneSanitizedSvg,
        serializeSafeSvg: serializeSafeSvg
    };
})(window, document);
