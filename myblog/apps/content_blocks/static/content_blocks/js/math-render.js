/**
 * LaTeX 数学公式按需懒加载与 KaTeX 渲染引擎 (math-render.js)
 * 仅当页面中存在 .cb-math 节点时才动态加载 KaTeX 脚本，零额外请求。
 */

(function () {
    "use strict";

    var katexLoadingPromise = null;

    function loadKatexScript() {
        if (window.katex) return Promise.resolve(window.katex);
        if (katexLoadingPromise) return katexLoadingPromise;

        katexLoadingPromise = new Promise(function (resolve, reject) {
            var script = document.createElement("script");
            script.src = "/static/vendor/katex/katex.min.js";
            script.async = true;
            script.onload = function () {
                resolve(window.katex);
            };
            script.onerror = function () {
                reject(new Error("KaTeX script load failed"));
            };
            document.head.appendChild(script);
        });

        return katexLoadingPromise;
    }

    function renderAllMath(root) {
        var container = root || document;
        var mathNodes = container.querySelectorAll(".cb-math:not([data-math-rendered])");
        if (!mathNodes || mathNodes.length === 0) return;

        loadKatexScript().then(function (katex) {
            mathNodes.forEach(function (node) {
                node.setAttribute("data-math-rendered", "true");
                var tex = node.getAttribute("data-tex");
                if (!tex) {
                    tex = node.textContent
                        .replace(/^\$\$|\$\$$/g, "")
                        .replace(/^\\\[|\\\]$/g, "")
                        .replace(/^\$|\$$/g, "")
                        .replace(/^\\\(|\\\)$/g, "")
                        .trim();
                }
                if (!tex) return;

                var isDisplay = node.classList.contains("cb-math-block") || node.classList.contains("cb-math-display");

                try {
                    katex.render(tex, node, {
                        displayMode: isDisplay,
                        throwOnError: false,
                        trust: false,
                        strict: "warn"
                    });
                } catch (e) {
                    console.warn("KaTeX render error on node:", e);
                }
            });
        }).catch(function (err) {
            console.error("Failed to render math:", err);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            renderAllMath(document);
        });
    } else {
        renderAllMath(document);
    }

    window.renderContentMath = renderAllMath;
})();
