/**
 * Mermaid 流程图统一渲染器与交互工具栏引擎 (mermaid-block.js)
 * 1. 按需懒加载：无图表零请求，存在时动态加载 vendor/mermaid/mermaid.min.js；
 * 2. 安全内联 SVG：securityLevel: strict + 严格剥离 script/foreignObject/on* 事件；
 * 3. 完整工具栏：缩放(+/-)、100%、自适应宽度、全屏弹窗、折叠、深浅主题切换、查看/复制源码、导出 SVG 与友好错误回退。
 */

(function () {
    "use strict";

    function setupMermaidBlock(block, renderer) {
        if (block.hasAttribute("data-cb-mermaid-rendered")) return;
        block.setAttribute("data-cb-mermaid-rendered", "true");

        var codeEl = block.querySelector("code[data-cb-mermaid-source]");
        if (!codeEl) return;
        var code = codeEl.textContent.trim();
        if (!code) return;

        var canvas = block.querySelector(".mermaid-diagram, .cb-mermaid-canvas");
        var statusEl = block.querySelector(".cb-mermaid-status");
        var sourceWrapper = block.querySelector(".cb-mermaid-source-wrapper");
        var initialTheme = renderer.normalizeTheme(block.getAttribute("data-theme") || "default");
        var currentTheme = initialTheme;
        var currentScale = 1.0;
        var currentSvgText = "";
        var renderSeq = 0;

        function renderDiagram(themeToUse) {
            var seq = ++renderSeq;
            var targetTheme = renderer.normalizeTheme(themeToUse);
            renderer.render(code, targetTheme).then(function (res) {
                if (seq !== renderSeq) return;
                currentTheme = res.theme;
                block.setAttribute("data-theme", res.theme);
                currentSvgText = res.svgHtml;
                canvas.innerHTML = "";
                canvas.appendChild(res.svgNode);
                if (statusEl) statusEl.hidden = true;
            }).catch(function (err) {
                if (seq !== renderSeq) return;
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

        renderDiagram(initialTheme);

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
                var nextTheme = currentTheme === "dark" ? (initialTheme === "dark" ? "default" : initialTheme) : "dark";
                renderDiagram(nextTheme);
            });
        }
        if (btnSource) {
            btnSource.addEventListener("click", function () {
                var isSplit = block.classList.toggle("is-split-view");
                btnSource.setAttribute("aria-pressed", String(isSplit));
                if (sourceWrapper) {
                    sourceWrapper.hidden = !isSplit;
                    if (isSplit) {
                        block.classList.remove("is-collapsed");
                        if (btnCollapse) {
                            btnCollapse.textContent = "▼";
                            btnCollapse.setAttribute("aria-expanded", "true");
                        }
                    }
                }
            });
        }
        if (btnCopy) {
            btnCopy.addEventListener("click", function () {
                var onCopied = function () {
                    var oldText = btnCopy.textContent;
                    btnCopy.textContent = "已复制 ✓";
                    setTimeout(function () { btnCopy.textContent = oldText; }, 2000);
                };
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(code).then(onCopied).catch(function () {
                        fallbackCopy(code, onCopied);
                    });
                } else {
                    fallbackCopy(code, onCopied);
                }
            });
        }

        function fallbackCopy(text, cb) {
            var ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.left = "-9999px";
            document.body.appendChild(ta);
            ta.select();
            try {
                if (document.execCommand("copy") && cb) cb();
            } finally {
                ta.remove();
            }
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

                var modalContent = modal.querySelector(".cb-mermaid-modal-content");
                modalContent.setAttribute("data-theme", currentTheme);
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

        var renderer = window.CBMermaidRenderer;
        if (!renderer) {
            blocks.forEach(function (block) {
                var statusEl = block.querySelector(".cb-mermaid-status");
                var sourceWrapper = block.querySelector(".cb-mermaid-source-wrapper");
                if (statusEl) {
                    statusEl.hidden = false;
                    statusEl.textContent = "Mermaid 公共渲染引擎未就绪，已显示源码";
                }
                if (sourceWrapper) sourceWrapper.hidden = false;
            });
            return;
        }

        blocks.forEach(function (block) {
            setupMermaidBlock(block, renderer);
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
