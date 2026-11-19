/**
 * Mermaid 流程图统一渲染器与交互工具栏引擎 (mermaid-block.js)
 * 1. 按需懒加载：无图表零请求，存在时动态加载 vendor/mermaid/mermaid.min.js；
 * 2. 安全内联 SVG：securityLevel: strict + 严格剥离 script/foreignObject/on* 事件；
 * 3. 完整工具栏：缩放(+/-)、100%、自适应宽度、全屏弹窗、折叠、深浅主题切换、查看/复制源码、导出 SVG 与友好错误回退。
 */

(function () {
    "use strict";

    var mermaidLoadingPromise = null;
    var diagramCounter = 0;

    function loadMermaidScript() {
        if (window.mermaid) return Promise.resolve(window.mermaid);
        if (mermaidLoadingPromise) return mermaidLoadingPromise;

        mermaidLoadingPromise = new Promise(function (resolve, reject) {
            var script = document.createElement("script");
            script.src = "/static/vendor/mermaid/mermaid.min.js";
            script.async = true;
            script.onload = function () {
                if (window.mermaid) {
                    window.mermaid.initialize({
                        startOnLoad: false,
                        securityLevel: "strict",
                        theme: "default",
                        htmlLabels: false,
                        fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif'
                    });
                    resolve(window.mermaid);
                } else {
                    reject(new Error("Mermaid global not found on script load"));
                }
            };
            script.onerror = function () {
                reject(new Error("Mermaid script load failed"));
            };
            document.head.appendChild(script);
        });

        return mermaidLoadingPromise;
    }

    function sanitizeSvgString(rawSvg) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(rawSvg, "image/svg+xml");
        var svg = doc.querySelector("svg");
        if (!svg) return null;

        // 移除所有恶意 script、foreignObject 与危险内联事件
        var dangerousElements = svg.querySelectorAll("script, foreignObject");
        dangerousElements.forEach(function (el) { el.remove(); });

        var allElements = svg.querySelectorAll("*");
        allElements.forEach(function (el) {
            var attrsToRemove = [];
            for (var i = 0; i < el.attributes.length; i++) {
                var attr = el.attributes[i];
                if (attr.name.toLowerCase().startsWith("on")) {
                    attrsToRemove.push(attr.name);
                }
                if ((attr.name === "href" || attr.name === "xlink:href") &&
                    attr.value.toLowerCase().trim().startsWith("javascript:")) {
                    attrsToRemove.push(attr.name);
                }
            }
            attrsToRemove.forEach(function (name) { el.removeAttribute(name); });
        });

        return svg;
    }

    function setupMermaidBlock(block, mermaid) {
        if (block.hasAttribute("data-cb-mermaid-rendered")) return;
        block.setAttribute("data-cb-mermaid-rendered", "true");

        var codeEl = block.querySelector("code[data-cb-mermaid-source]");
        if (!codeEl) return;
        var code = codeEl.textContent.trim();
        if (!code) return;

        var canvas = block.querySelector(".mermaid-diagram, .cb-mermaid-canvas");
        var statusEl = block.querySelector(".cb-mermaid-status");
        var sourceWrapper = block.querySelector(".cb-mermaid-source-wrapper");
        var currentTheme = block.getAttribute("data-theme") || "default";
        var currentScale = 1.0;
        var currentSvgText = "";

        diagramCounter++;
        var svgId = "cb_mermaid_svg_" + Date.now() + "_" + diagramCounter;

        function renderDiagram(themeToUse) {
            mermaid.initialize({
                securityLevel: "strict",
                theme: themeToUse,
                htmlLabels: false
            });

            mermaid.render(svgId, code).then(function (result) {
                var cleanSvgNode = sanitizeSvgString(result.svg);
                if (!cleanSvgNode) {
                    throw new Error("SVG 图表净化后格式无效");
                }
                currentSvgText = cleanSvgNode.outerHTML;
                canvas.innerHTML = "";
                canvas.appendChild(cleanSvgNode);
                if (statusEl) statusEl.hidden = true;
            }).catch(function (err) {
                console.warn("Mermaid render error:", err);
                if (statusEl) {
                    statusEl.hidden = false;
                    statusEl.textContent = "流程图解析异常: " + (err.message || "请检查语法是否正确");
                }
                if (sourceWrapper) {
                    sourceWrapper.hidden = false;
                }
            });
        }

        renderDiagram(currentTheme);

        // 工具栏交互绑定
        var btnZoomIn = block.querySelector(".cb-btn-zoom-in");
        var btnZoomOut = block.querySelector(".cb-btn-zoom-out");
        var btnZoomReset = block.querySelector(".cb-btn-zoom-reset");
        var btnFit = block.querySelector(".cb-btn-fit");
        var btnTheme = block.querySelector(".cb-btn-theme");
        var btnSource = block.querySelector(".cb-btn-source");
        var btnCopy = block.querySelector(".cb-btn-copy");
        var btnExport = block.querySelector(".cb-btn-export");
        var btnFullscreen = block.querySelector(".cb-btn-fullscreen");
        var btnCollapse = block.querySelector(".cb-btn-collapse");

        function applyScale(scale) {
            currentScale = Math.min(3.0, Math.max(0.4, scale));
            canvas.style.transform = "scale(" + currentScale + ")";
        }

        if (btnZoomIn) {
            btnZoomIn.addEventListener("click", function () { applyScale(currentScale + 0.2); });
        }
        if (btnZoomOut) {
            btnZoomOut.addEventListener("click", function () { applyScale(currentScale - 0.2); });
        }
        if (btnZoomReset) {
            btnZoomReset.addEventListener("click", function () { applyScale(1.0); });
        }
        if (btnFit) {
            btnFit.addEventListener("click", function () {
                currentScale = 1.0;
                canvas.style.transform = "none";
                canvas.style.width = "100%";
            });
        }
        if (btnTheme) {
            btnTheme.addEventListener("click", function () {
                currentTheme = currentTheme === "dark" ? "default" : "dark";
                block.setAttribute("data-theme", currentTheme);
                renderDiagram(currentTheme);
            });
        }
        if (btnSource) {
            btnSource.addEventListener("click", function () {
                if (sourceWrapper) sourceWrapper.hidden = !sourceWrapper.hidden;
            });
        }
        if (btnCopy) {
            btnCopy.addEventListener("click", function () {
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(code).then(function () {
                        var oldText = btnCopy.textContent;
                        btnCopy.textContent = "已复制 ✓";
                        setTimeout(function () { btnCopy.textContent = oldText; }, 2000);
                    });
                }
            });
        }
        if (btnExport) {
            btnExport.addEventListener("click", function () {
                if (!currentSvgText) return;
                var blob = new Blob([currentSvgText], { type: "image/svg+xml;charset=utf-8" });
                var url = URL.createObjectURL(blob);
                var a = document.createElement("a");
                a.href = url;
                a.download = "mermaid-diagram.svg";
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            });
        }
        if (btnCollapse) {
            btnCollapse.addEventListener("click", function () {
                var isCollapsed = block.classList.toggle("is-collapsed");
                btnCollapse.textContent = isCollapsed ? "▲" : "▼";
                btnCollapse.setAttribute("aria-expanded", String(!isCollapsed));
            });
        }
        if (btnFullscreen) {
            btnFullscreen.addEventListener("click", function () {
                var modal = document.createElement("dialog");
                modal.className = "cb-mermaid-modal";
                modal.innerHTML = '<div class="cb-mermaid-modal-content">' +
                    '<button type="button" class="cb-mermaid-modal-close" aria-label="关闭 (Esc)">×</button>' +
                    '<div class="cb-mermaid-modal-canvas"></div>' +
                    '</div>';

                var modalCanvas = modal.querySelector(".cb-mermaid-modal-canvas");
                modalCanvas.innerHTML = currentSvgText;

                function closeModal() {
                    modal.close();
                    modal.remove();
                    btnFullscreen.focus();
                }

                modal.querySelector(".cb-mermaid-modal-close").addEventListener("click", closeModal);
                modal.addEventListener("click", function (e) {
                    if (e.target === modal) closeModal();
                });
                modal.addEventListener("keydown", function (e) {
                    if (e.key === "Escape") {
                        e.preventDefault();
                        closeModal();
                    }
                });

                document.body.appendChild(modal);
                modal.showModal();
            });
        }
    }

    function initAllMermaid(root) {
        var container = root || document;
        var blocks = container.querySelectorAll("[data-cb-mermaid]");
        if (!blocks || blocks.length === 0) return;

        loadMermaidScript().then(function (mermaid) {
            blocks.forEach(function (block) {
                setupMermaidBlock(block, mermaid);
            });
        }).catch(function (err) {
            console.error("Failed to initialize Mermaid:", err);
            blocks.forEach(function (block) {
                var statusEl = block.querySelector(".cb-mermaid-status");
                var sourceWrapper = block.querySelector(".cb-mermaid-source-wrapper");
                if (statusEl) {
                    statusEl.hidden = false;
                    statusEl.textContent = "Mermaid 引擎加载失败，已显示源码";
                }
                if (sourceWrapper) {
                    sourceWrapper.hidden = false;
                }
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            initAllMermaid(document);
        });
    } else {
        initAllMermaid(document);
    }

    window.initMermaidBlocks = initAllMermaid;
})();
