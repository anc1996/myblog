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
        var declaredTheme = renderer.normalizeTheme(block.getAttribute("data-theme") || "default");
        if (declaredTheme === "follow-site") {
            block.setAttribute("data-mermaid-declared-theme", "follow-site");
        }
        var initialTheme = declaredTheme;
        var currentTheme = initialTheme;
        var manualThemeOverride = null;
        var currentScale = 1.0;
        var currentSvgText = "";
        var renderSeq = 0;
        var hasRenderedSvg = false;
        var viewNotified = false;
        var activeModalRefs = null;

        function getBlockTargetKey() {
            var root = block.closest("[data-cb-block-id]");
            var bid = root ? (root.getAttribute("data-cb-block-id") || "") : "";
            if (bid) return "mermaid:" + bid.slice(0, 36);
            var all = document.querySelectorAll("[data-cb-mermaid]");
            for (var i = 0; i < all.length; i++) {
                if (all[i] === block) return "mermaid:" + i;
            }
            return "mermaid:0";
        }

        function maybeNotifyMermaidView(forceVisible) {
            if (viewNotified || !hasRenderedSvg) return;
            if (!forceVisible && block.classList.contains("is-collapsed")) return;
            viewNotified = true;
            try {
                window.dispatchEvent(new CustomEvent("myblog:analytics:conversion", {
                    detail: { conversion_key: "mermaid_view", target_key: getBlockTargetKey() }
                }));
            } catch (_e) {}
        }

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
                hasRenderedSvg = true;
                if (activeModalRefs && document.body.contains(activeModalRefs.modal)) {
                    if (typeof activeModalRefs.syncSvg === "function") {
                        activeModalRefs.syncSvg(res.svgHtml, res.theme);
                    } else if (activeModalRefs.modalContent && activeModalRefs.modalCanvas) {
                        activeModalRefs.modalContent.setAttribute("data-theme", res.theme);
                        activeModalRefs.modalCanvas.innerHTML = res.svgHtml;
                    }
                }
                try {
                    var rect = canvas.getBoundingClientRect();
                    var vh = window.innerHeight || document.documentElement.clientHeight || 0;
                    if (rect.height > 0 && rect.top < vh && rect.bottom > 0 && !block.classList.contains("is-collapsed")) {
                        maybeNotifyMermaidView(false);
                    }
                } catch (_e) {}
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

        if (declaredTheme === "follow-site") {
            window.addEventListener("myblog:theme:change", function () {
                if (!manualThemeOverride && block.isConnected) {
                    renderDiagram("follow-site");
                }
            });
        }

        if (canvas && typeof window.IntersectionObserver === "function") {
            try {
                var viewObs = new window.IntersectionObserver(function (entries) {
                    for (var i = 0; i < entries.length; i++) {
                        if (entries[i].isIntersecting) {
                            maybeNotifyMermaidView(false);
                        }
                    }
                }, { threshold: 0.25 });
                viewObs.observe(canvas);
            } catch (_e) {}
        }

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
                var nextTheme = currentTheme === "dark" ? (declaredTheme === "follow-site" || declaredTheme === "dark" ? "default" : declaredTheme) : "dark";
                manualThemeOverride = nextTheme;
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

        function exportMermaidPng2x(svgText, themeName) {
            if (!svgText) return;
            try {
                var parser = new DOMParser();
                var doc = parser.parseFromString(svgText, "image/svg+xml");
                var svgNode = doc.querySelector("svg");
                if (!svgNode) return;
                var vb = svgNode.getAttribute("viewBox");
                var w = 0;
                var h = 0;
                if (vb) {
                    var parts = vb.trim().split(/[\s,]+/);
                    if (parts.length === 4) {
                        w = parseFloat(parts[2]) || 0;
                        h = parseFloat(parts[3]) || 0;
                    }
                }
                if (!w || !h) {
                    w = parseFloat(svgNode.getAttribute("width")) || 960;
                    h = parseFloat(svgNode.getAttribute("height")) || 540;
                }
                w = Math.max(200, Math.min(4096, Math.ceil(w)));
                h = Math.max(150, Math.min(4096, Math.ceil(h)));
                svgNode.setAttribute("width", String(w));
                svgNode.setAttribute("height", String(h));
                svgNode.setAttribute("xmlns", "http://www.w3.org/2000/svg");
                var serialized = new XMLSerializer().serializeToString(svgNode);
                var svgDataUrl = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(serialized);
                var img = new Image();
                img.onload = function () {
                    var dpr = 2;
                    var pad = 24;
                    var offCanvas = document.createElement("canvas");
                    offCanvas.width = (w + pad * 2) * dpr;
                    offCanvas.height = (h + pad * 2) * dpr;
                    var ctx = offCanvas.getContext("2d");
                    if (!ctx) return;
                    ctx.scale(dpr, dpr);
                    var rootStyle = getComputedStyle(document.documentElement);
                    var bgFill = themeName === "dark"
                        ? (rootStyle.getPropertyValue("--palette-mist-950").trim() || "rgb(15, 23, 42)")
                        : (rootStyle.getPropertyValue("--palette-mist-50").trim() || "rgb(248, 250, 252)");
                    ctx.fillStyle = bgFill;
                    ctx.fillRect(0, 0, w + pad * 2, h + pad * 2);
                    ctx.drawImage(img, pad, pad, w, h);
                    try {
                        offCanvas.toBlob(function (pngBlob) {
                            if (!pngBlob) return;
                            var pngUrl = URL.createObjectURL(pngBlob);
                            var link = document.createElement("a");
                            link.href = pngUrl;
                            link.download = "mermaid-diagram@2x.png";
                            document.body.appendChild(link);
                            link.click();
                            document.body.removeChild(link);
                            URL.revokeObjectURL(pngUrl);
                        }, "image/png");
                    } catch (_err) {
                        // 浏览器若因外链资源判定 Canvas 受污染，安全回退为导出 SVG
                        if (btnExport) btnExport.click();
                    }
                };
                img.src = svgDataUrl;
            } catch (_e) {
                if (btnExport) btnExport.click();
            }
        }

        if (btnCollapse) {
            btnCollapse.addEventListener("click", function () {
                var isCollapsed = block.classList.toggle("is-collapsed");
                btnCollapse.textContent = isCollapsed ? "▲" : "▼";
                btnCollapse.setAttribute("aria-expanded", String(!isCollapsed));
                if (!isCollapsed) {
                    maybeNotifyMermaidView(false);
                }
            });
        }
        if (btnFullscreen) {
            btnFullscreen.addEventListener("click", function () {
                maybeNotifyMermaidView(true);
                var titleNode = block.querySelector(".cb-mermaid-title");
                var diagramTitle = (titleNode ? titleNode.textContent.trim() : "") || "流程图全屏查看";

                if (window.CBStageViewer && typeof window.CBStageViewer.open === "function") {
                    var hitNodes = [];
                    var hitIndex = -1;
                    var lastQuery = "";

                    function parseSvgNode(svgHtml) {
                        var wrap = document.createElement("div");
                        wrap.innerHTML = svgHtml || "";
                        var svg = wrap.querySelector("svg");
                        if (!svg) return { node: wrap, size: { width: 0, height: 0 } };
                        var vb = svg.viewBox && svg.viewBox.baseVal;
                        var w = (vb && vb.width > 0) ? vb.width : (parseFloat(svg.getAttribute("width")) || 800);
                        var h = (vb && vb.height > 0) ? vb.height : (parseFloat(svg.getAttribute("height")) || 480);
                        svg.setAttribute("width", String(Math.ceil(w)));
                        svg.setAttribute("height", String(Math.ceil(h)));
                        svg.style.maxWidth = "none";
                        return { node: svg, size: { width: w, height: h } };
                    }

                    var viewer = window.CBStageViewer.open({
                        title: diagramTitle,
                        ariaLabel: diagramTitle + "（全屏查看）",
                        minScale: 0.2,
                        maxScale: 8.0,
                        maxFitScale: 1.8,
                        initialBackdrop: currentTheme === "dark" ? "dark" : "light",
                        showFooter: true,
                        hintText: "滚轮定点缩放 · 拖拽平移 · 搜索节点自动居中 · Esc 退出",
                        triggerEl: btnFullscreen,
                        onClose: function () {
                            activeModalRefs = null;
                        },
                    });

                    // 构建 Mermaid 全屏专属工具栏：节点搜索 + 上下命中 + 主题切换 + SVG/PNG 导出 + 复制源码
                    var searchGroup = document.createElement("div");
                    searchGroup.className = "cb-stage-search-group";
                    var searchInput = document.createElement("input");
                    searchInput.type = "search";
                    searchInput.className = "cb-stage-search-input";
                    searchInput.placeholder = "搜索节点…";
                    searchInput.setAttribute("aria-label", "搜索流程图节点");
                    searchInput.setAttribute("data-mermaid-node-search", "1");

                    var searchCount = document.createElement("span");
                    searchCount.className = "cb-stage-search-count";
                    searchCount.textContent = "";

                    var btnPrevHit = document.createElement("button");
                    btnPrevHit.type = "button";
                    btnPrevHit.className = "cb-stage-search-nav";
                    btnPrevHit.textContent = "↑";
                    btnPrevHit.title = "上一个匹配节点";
                    btnPrevHit.disabled = true;

                    var btnNextHit = document.createElement("button");
                    btnNextHit.type = "button";
                    btnNextHit.className = "cb-stage-search-nav";
                    btnNextHit.textContent = "↓";
                    btnNextHit.title = "下一个匹配节点";
                    btnNextHit.disabled = true;

                    searchGroup.appendChild(searchInput);
                    searchGroup.appendChild(searchCount);
                    searchGroup.appendChild(btnPrevHit);
                    searchGroup.appendChild(btnNextHit);
                    viewer.extraSlot.appendChild(searchGroup);

                    function clearSearchHighlights() {
                        viewer.canvas.querySelectorAll(".cb-mermaid-search-hit, .cb-mermaid-search-current").forEach(function (el) {
                            el.classList.remove("cb-mermaid-search-hit", "cb-mermaid-search-current");
                        });
                        hitNodes = [];
                        hitIndex = -1;
                        searchCount.textContent = "";
                        btnPrevHit.disabled = true;
                        btnNextHit.disabled = true;
                    }

                    function focusHit(idx) {
                        if (!hitNodes.length) return;
                        hitIndex = ((idx % hitNodes.length) + hitNodes.length) % hitNodes.length;
                        hitNodes.forEach(function (el, i) {
                            el.classList.toggle("cb-mermaid-search-current", i === hitIndex);
                        });
                        searchCount.textContent = (hitIndex + 1) + "/" + hitNodes.length;
                        viewer.panToElement(hitNodes[hitIndex]);
                    }

                    function runNodeSearch(query, shouldCenter) {
                        clearSearchHighlights();
                        var q = (query || "").trim().toLowerCase();
                        lastQuery = q;
                        if (!q) return;
                        var candidates = viewer.canvas.querySelectorAll("svg text, svg .nodeLabel, svg .edgeLabel, svg .cluster-label");
                        var seen = [];
                        candidates.forEach(function (el) {
                            var txt = (el.textContent || "").trim().toLowerCase();
                            if (!txt || txt.indexOf(q) === -1) return;
                            var target = el.closest("g.node, g.cluster, g.edgeLabel, g.label") || el;
                            if (seen.indexOf(target) === -1) {
                                seen.push(target);
                                target.classList.add("cb-mermaid-search-hit");
                            }
                        });
                        hitNodes = seen;
                        if (hitNodes.length) {
                            btnPrevHit.disabled = false;
                            btnNextHit.disabled = false;
                            if (shouldCenter) {
                                focusHit(0);
                            } else {
                                searchCount.textContent = "0/" + hitNodes.length;
                            }
                        } else {
                            searchCount.textContent = "0/0";
                        }
                    }

                    searchInput.addEventListener("input", function () {
                        runNodeSearch(searchInput.value, true);
                    });
                    searchInput.addEventListener("keydown", function (evt) {
                        if (evt.key === "Enter" && hitNodes.length) {
                            evt.preventDefault();
                            focusHit(evt.shiftKey ? hitIndex - 1 : hitIndex + 1);
                        }
                    });
                    btnPrevHit.addEventListener("click", function () { focusHit(hitIndex - 1); });
                    btnNextHit.addEventListener("click", function () { focusHit(hitIndex + 1); });

                    var btnStageTheme = window.CBStageViewer.createToolbarButton({
                        label: currentTheme === "dark" ? "浅色图" : "深色图",
                        title: "切换全屏流程图主题",
                        onClick: function () {
                            var nextTheme = currentTheme === "dark"
                                ? (declaredTheme === "follow-site" || declaredTheme === "dark" ? "default" : declaredTheme)
                                : "dark";
                            manualThemeOverride = nextTheme;
                            renderDiagram(nextTheme);
                        },
                    });
                    var btnStageSvg = window.CBStageViewer.createToolbarButton({
                        label: "SVG",
                        title: "导出矢量 SVG 文件",
                        onClick: function () {
                            if (btnExport) btnExport.click();
                        },
                    });
                    var btnStagePng = window.CBStageViewer.createToolbarButton({
                        label: "PNG 2x",
                        title: "导出 2 倍高清 PNG 图片",
                        onClick: function () {
                            exportMermaidPng2x(currentSvgText, currentTheme);
                        },
                    });
                    var btnStageCopy = window.CBStageViewer.createToolbarButton({
                        label: "复制源码",
                        title: "复制 Mermaid 源代码",
                        onClick: function () {
                            var span = btnStageCopy.querySelector("span");
                            var onDone = function () {
                                if (!span) return;
                                var old = span.textContent;
                                span.textContent = "已复制";
                                setTimeout(function () { span.textContent = old; }, 1800);
                            };
                            if (navigator.clipboard && window.isSecureContext) {
                                navigator.clipboard.writeText(code).then(onDone).catch(function () {
                                    fallbackCopy(code, onDone);
                                });
                            } else {
                                fallbackCopy(code, onDone);
                            }
                        },
                    });

                    viewer.extraSlot.appendChild(btnStageTheme);
                    viewer.extraSlot.appendChild(btnStageSvg);
                    viewer.extraSlot.appendChild(btnStagePng);
                    viewer.extraSlot.appendChild(btnStageCopy);

                    function mountSvgIntoViewer(svgHtml, themeName, preserveScale) {
                        var parsed = parseSvgNode(svgHtml);
                        var mode = preserveScale ? "actual" : "fit";
                        viewer.setContent(parsed.node, parsed.size, mode);
                        viewer.setBackdrop(themeName === "dark" ? "dark" : "light");
                        var themeSpan = btnStageTheme.querySelector("span");
                        if (themeSpan) {
                            themeSpan.textContent = themeName === "dark" ? "浅色图" : "深色图";
                        }
                        if (lastQuery) {
                            window.requestAnimationFrame(function () {
                                runNodeSearch(lastQuery, false);
                            });
                        }
                    }

                    mountSvgIntoViewer(currentSvgText, currentTheme, false);
                    activeModalRefs = {
                        modal: viewer.dialog,
                        modalContent: viewer.dialog,
                        modalCanvas: viewer.canvas,
                        syncSvg: function (svgHtml, themeName) {
                            mountSvgIntoViewer(svgHtml, themeName, false);
                        },
                    };
                    return;
                }

                // 降级回退：当公共 CBStageViewer 未加载时保留轻量原生弹窗
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
                activeModalRefs = {
                    modal: modal,
                    modalContent: modalContent,
                    modalCanvas: modalCanvas
                };

                function closeModal() {
                    activeModalRefs = null;
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
