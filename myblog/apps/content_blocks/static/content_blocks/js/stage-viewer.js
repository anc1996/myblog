/**
 * 全站公共沉浸式画布查看器引擎 (CBStageViewer)
 * 统一封装 vendor/panzoom/panzoom.min.js（按需懒加载），为 Mermaid 复杂流程图全屏与图片灯箱提供：
 * - 光标定点滚轮缩放、抓手拖拽、手机双指捏合、双击缩放切换；
 * - 适配窗口 (Fit)、适配宽度长图阅读 (Fit Width)、1:1 原尺寸、底色切换（暗 / 浅 / 棋盘格）；
 * - 任意子元素平滑居中定位 (panToElement，供 Mermaid 节点搜索使用)；
 * - 全站 <dialog> 互斥、焦点恢复与无外部依赖优雅降级。
 */

(function (window, document) {
    "use strict";

    var panzoomPromise = null;
    var cachedPanzoomUrl = "";

    function discoverPanzoomUrl() {
        if (cachedPanzoomUrl) return cachedPanzoomUrl;
        var scriptEl = document.querySelector("script[data-panzoom-url]");
        if (scriptEl) {
            cachedPanzoomUrl = scriptEl.getAttribute("data-panzoom-url") || "";
        }
        if (!cachedPanzoomUrl) {
            cachedPanzoomUrl = "/static/vendor/panzoom/panzoom.min.js";
        }
        return cachedPanzoomUrl;
    }

    function loadPanzoom() {
        if (typeof window.Panzoom === "function") {
            return Promise.resolve(window.Panzoom);
        }
        if (panzoomPromise) {
            return panzoomPromise;
        }
        panzoomPromise = new Promise(function (resolve) {
            var src = discoverPanzoomUrl();
            var script = document.createElement("script");
            script.src = src;
            script.async = true;
            script.onload = function () {
                resolve(typeof window.Panzoom === "function" ? window.Panzoom : null);
            };
            script.onerror = function () {
                panzoomPromise = null;
                resolve(null);
            };
            document.head.appendChild(script);
        });
        return panzoomPromise;
    }

    function closeOtherDialogs(exceptDialog) {
        document.querySelectorAll("dialog[open]").forEach(function (dlg) {
            if (dlg === exceptDialog) return;
            if (typeof dlg.close === "function") {
                dlg.close();
            } else {
                dlg.removeAttribute("open");
            }
            if (dlg.id) {
                document.querySelectorAll('[aria-controls="' + dlg.id + '"]').forEach(function (ctrl) {
                    ctrl.setAttribute("aria-expanded", "false");
                });
            }
        });
    }

    function makeSvgIcon(pathMarkup) {
        var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("width", "15");
        svg.setAttribute("height", "15");
        svg.setAttribute("fill", "none");
        svg.setAttribute("stroke", "currentColor");
        svg.setAttribute("stroke-width", "1.8");
        svg.setAttribute("stroke-linecap", "round");
        svg.setAttribute("stroke-linejoin", "round");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("class", "cb-stage-icon");
        svg.innerHTML = pathMarkup;
        return svg;
    }

    var ICONS = {
        zoomOut: '<circle cx="11" cy="11" r="8"/><line x1="21" x2="16.65" y1="21" y2="16.65"/><line x1="8" x2="14" y1="11" y2="11"/>',
        zoomIn: '<circle cx="11" cy="11" r="8"/><line x1="21" x2="16.65" y1="21" y2="16.65"/><line x1="11" x2="11" y1="8" y2="14"/><line x1="8" x2="14" y1="11" y2="11"/>',
        fit: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
        fitWidth: '<path d="M21 12H3"/><path d="m7 8-4 4 4 4"/><path d="m17 8 4 4-4 4"/>',
        backdrop: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 3v18"/><path d="M12 12h9"/>',
        close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
        prev: '<path d="m15 18-6-6 6-6"/>',
        next: '<path d="m9 18 6-6-6-6"/>',
    };

    var BACKDROP_ORDER = ["dark", "light", "checker"];
    var BACKDROP_LABELS = {
        dark: "暗底",
        light: "浅底",
        checker: "网格",
    };

    function createToolbarButton(opts) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "cb-stage-btn" + (opts.className ? (" " + opts.className) : "");
        if (opts.title) btn.title = opts.title;
        if (opts.ariaLabel) btn.setAttribute("aria-label", opts.ariaLabel);
        if (opts.icon) btn.appendChild(makeSvgIcon(opts.icon));
        if (opts.label) {
            var span = document.createElement("span");
            span.textContent = opts.label;
            btn.appendChild(span);
        }
        if (opts.onClick) {
            btn.addEventListener("click", opts.onClick);
        }
        return btn;
    }

    function openStageViewer(options) {
        var opts = options || {};
        var minScale = typeof opts.minScale === "number" ? opts.minScale : 0.2;
        var maxScale = typeof opts.maxScale === "number" ? opts.maxScale : 6.0;
        var initialBg = BACKDROP_ORDER.indexOf(opts.initialBackdrop) !== -1
            ? opts.initialBackdrop
            : (document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light");
        var restoreFocusEl = opts.triggerEl || document.activeElement;

        var dialog = document.createElement("dialog");
        dialog.className = "cb-stage-dialog";
        dialog.setAttribute("data-stage-bg", initialBg);
        dialog.setAttribute("aria-label", opts.ariaLabel || opts.title || "全屏查看器");

        // 1. 顶部统一工具栏
        var toolbar = document.createElement("div");
        toolbar.className = "cb-stage-toolbar";

        var leftGroup = document.createElement("div");
        leftGroup.className = "cb-stage-toolbar-left";
        var titleEl = document.createElement("h2");
        titleEl.className = "cb-stage-title";
        titleEl.textContent = opts.title || "全屏预览";
        var metaEl = document.createElement("span");
        metaEl.className = "cb-stage-meta";
        metaEl.textContent = opts.meta || "";
        leftGroup.appendChild(titleEl);
        leftGroup.appendChild(metaEl);

        var centerGroup = document.createElement("div");
        centerGroup.className = "cb-stage-toolbar-center";

        var btnZoomOut = createToolbarButton({
            icon: ICONS.zoomOut,
            title: "缩小 (-)",
            ariaLabel: "缩小",
            onClick: function () { controller.zoomOut(); },
        });
        var scaleLabel = document.createElement("span");
        scaleLabel.className = "cb-stage-scale-label";
        scaleLabel.textContent = "100%";
        var btnZoomIn = createToolbarButton({
            icon: ICONS.zoomIn,
            title: "放大 (+)",
            ariaLabel: "放大",
            onClick: function () { controller.zoomIn(); },
        });
        var btnFit = createToolbarButton({
            icon: ICONS.fit,
            label: "适配",
            title: "自适应窗口 (0)",
            ariaLabel: "自适应窗口",
            onClick: function () { controller.fit(); },
        });
        var btnActual = createToolbarButton({
            label: "1:1",
            title: "原始尺寸 (1)",
            ariaLabel: "原始尺寸",
            onClick: function () { controller.resetActual(); },
        });

        centerGroup.appendChild(btnZoomOut);
        centerGroup.appendChild(scaleLabel);
        centerGroup.appendChild(btnZoomIn);
        centerGroup.appendChild(btnFit);

        if (opts.enableFitWidth) {
            var btnFitWidth = createToolbarButton({
                icon: ICONS.fitWidth,
                label: "适宽",
                title: "适配宽度（适合长图阅读）",
                ariaLabel: "适配宽度",
                onClick: function () { controller.fitWidth(); },
            });
            centerGroup.appendChild(btnFitWidth);
        }
        centerGroup.appendChild(btnActual);

        var btnBackdrop = createToolbarButton({
            icon: ICONS.backdrop,
            label: BACKDROP_LABELS[initialBg],
            title: "切换画布底色（暗底 / 浅底 / 棋盘格）",
            ariaLabel: "切换画布底色",
            onClick: function () {
                var cur = dialog.getAttribute("data-stage-bg") || "dark";
                var nextIdx = (BACKDROP_ORDER.indexOf(cur) + 1) % BACKDROP_ORDER.length;
                controller.setBackdrop(BACKDROP_ORDER[nextIdx]);
            },
        });
        centerGroup.appendChild(btnBackdrop);

        var extraSlot = document.createElement("div");
        extraSlot.className = "cb-stage-toolbar-center";
        centerGroup.appendChild(extraSlot);

        var rightGroup = document.createElement("div");
        rightGroup.className = "cb-stage-toolbar-right";
        var btnClose = createToolbarButton({
            icon: ICONS.close,
            className: "cb-stage-close-btn",
            title: "关闭 (Esc)",
            ariaLabel: "关闭全屏",
            onClick: function () { controller.close(); },
        });
        rightGroup.appendChild(btnClose);

        toolbar.appendChild(leftGroup);
        toolbar.appendChild(centerGroup);
        toolbar.appendChild(rightGroup);

        // 2. 主画布视口与 Panzoom 挂载点
        var viewport = document.createElement("div");
        viewport.className = "cb-stage-viewport";

        var canvas = document.createElement("div");
        canvas.className = "cb-stage-canvas";
        viewport.appendChild(canvas);

        var prevNavBtn = document.createElement("button");
        prevNavBtn.type = "button";
        prevNavBtn.className = "cb-stage-nav-btn cb-stage-nav-prev";
        prevNavBtn.setAttribute("aria-label", "上一张 (←)");
        prevNavBtn.title = "上一张 (←)";
        prevNavBtn.appendChild(makeSvgIcon(ICONS.prev));
        prevNavBtn.hidden = true;

        var nextNavBtn = document.createElement("button");
        nextNavBtn.type = "button";
        nextNavBtn.className = "cb-stage-nav-btn cb-stage-nav-next";
        nextNavBtn.setAttribute("aria-label", "下一张 (→)");
        nextNavBtn.title = "下一张 (→)";
        nextNavBtn.appendChild(makeSvgIcon(ICONS.next));
        nextNavBtn.hidden = true;

        if (typeof opts.onPrev === "function") {
            prevNavBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                opts.onPrev();
            });
        }
        if (typeof opts.onNext === "function") {
            nextNavBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                opts.onNext();
            });
        }

        viewport.appendChild(prevNavBtn);
        viewport.appendChild(nextNavBtn);

        // 3. 底部说明与缩略图条
        var footer = document.createElement("div");
        footer.className = "cb-stage-footer";
        var captionRow = document.createElement("div");
        captionRow.className = "cb-stage-caption-row";
        var captionEl = document.createElement("div");
        captionEl.className = "cb-stage-caption";
        var hintEl = document.createElement("div");
        hintEl.className = "cb-stage-hint";
        hintEl.textContent = opts.hintText || "滚轮缩放 · 拖拽平移 · 双击切换 · Esc 退出";
        captionRow.appendChild(captionEl);
        captionRow.appendChild(hintEl);

        var thumbsEl = document.createElement("div");
        thumbsEl.className = "cb-stage-thumbs";
        thumbsEl.hidden = true;

        footer.appendChild(captionRow);
        footer.appendChild(thumbsEl);
        if (!opts.showFooter) {
            footer.hidden = true;
        }

        dialog.appendChild(toolbar);
        dialog.appendChild(viewport);
        dialog.appendChild(footer);
        document.body.appendChild(dialog);

        var panzoomInstance = null;
        var fallbackScale = 1.0;
        var contentNaturalSize = { width: 0, height: 0 };
        var pendingFitMode = "fit";
        var isClosed = false;
        var lastFitScale = 1.0;

        function updateScaleLabel(scaleVal) {
            var pct = Math.round((scaleVal || 1) * 100);
            scaleLabel.textContent = pct + "%";
        }

        function getCurrentScale() {
            if (panzoomInstance && typeof panzoomInstance.getScale === "function") {
                return panzoomInstance.getScale();
            }
            return fallbackScale;
        }

        function applyTransform(scale, x, y, animate) {
            var clamped = Math.min(maxScale, Math.max(minScale, scale));
            if (panzoomInstance) {
                panzoomInstance.zoom(clamped, { animate: Boolean(animate), force: true });
                panzoomInstance.pan(x, y, { animate: Boolean(animate), force: true });
                updateScaleLabel(panzoomInstance.getScale());
            } else {
                fallbackScale = clamped;
                canvas.style.transform = "translate(" + Math.round(x) + "px, " + Math.round(y) + "px) scale(" + clamped + ")";
                updateScaleLabel(clamped);
            }
        }

        function measureContentSize() {
            if (contentNaturalSize.width > 0 && contentNaturalSize.height > 0) {
                return contentNaturalSize;
            }
            var child = canvas.firstElementChild;
            if (!child) return { width: 0, height: 0 };
            if (child.tagName && child.tagName.toLowerCase() === "svg") {
                var vb = child.viewBox && child.viewBox.baseVal;
                if (vb && vb.width > 0 && vb.height > 0) {
                    return { width: vb.width, height: vb.height };
                }
            }
            var w = child.offsetWidth || child.clientWidth || 0;
            var h = child.offsetHeight || child.clientHeight || 0;
            return { width: w, height: h };
        }

        function fitToViewport() {
            pendingFitMode = "fit";
            var vpRect = viewport.getBoundingClientRect();
            var size = measureContentSize();
            if (!vpRect.width || !vpRect.height || !size.width || !size.height) {
                lastFitScale = 1.0;
                applyTransform(1.0, 0, 0, false);
                return;
            }
            var availW = Math.max(120, vpRect.width - 48);
            var availH = Math.max(120, vpRect.height - 48);
            var scale = Math.min(availW / size.width, availH / size.height, opts.maxFitScale || 1.5);
            scale = Math.min(maxScale, Math.max(minScale, scale));
            lastFitScale = scale;
            applyTransform(scale, 0, 0, false);
        }

        function fitToWidth() {
            pendingFitMode = "fit-width";
            var vpRect = viewport.getBoundingClientRect();
            var size = measureContentSize();
            if (!vpRect.width || !vpRect.height || !size.width || !size.height) {
                fitToViewport();
                return;
            }
            var availW = Math.max(120, vpRect.width - 56);
            var scale = Math.min(maxScale, Math.max(minScale, availW / size.width));
            var scaledH = size.height * scale;
            var panY = 0;
            if (scaledH > vpRect.height - 32) {
                panY = (scaledH - vpRect.height) / (2 * scale) + (16 / scale);
            }
            applyTransform(scale, 0, panY, false);
        }

        function panToElement(targetEl) {
            if (!targetEl || !canvas.contains(targetEl)) return;
            var vpRect = viewport.getBoundingClientRect();
            var elRect = targetEl.getBoundingClientRect();
            if (!vpRect.width || !vpRect.height || (!elRect.width && !elRect.height)) return;

            var curScale = getCurrentScale();
            var targetScale = curScale < 1.25 ? Math.min(maxScale, 1.35) : curScale;
            var curPan = panzoomInstance && panzoomInstance.getPan ? panzoomInstance.getPan() : { x: 0, y: 0 };

            var vpCenterX = vpRect.left + vpRect.width / 2;
            var vpCenterY = vpRect.top + vpRect.height / 2;
            var elCenterX = elRect.left + elRect.width / 2;
            var elCenterY = elRect.top + elRect.height / 2;

            var dxScreen = vpCenterX - elCenterX;
            var dyScreen = vpCenterY - elCenterY;
            var nextX = curPan.x + (dxScreen / curScale);
            var nextY = curPan.y + (dyScreen / curScale);

            applyTransform(targetScale, nextX, nextY, true);
        }

        function initPanzoomEngine() {
            return loadPanzoom().then(function (PanzoomCtor) {
                if (isClosed) return;
                if (panzoomInstance) {
                    try { panzoomInstance.destroy(); } catch (_e) {}
                    panzoomInstance = null;
                }
                if (typeof PanzoomCtor === "function") {
                    panzoomInstance = PanzoomCtor(canvas, {
                        minScale: minScale,
                        maxScale: maxScale,
                        step: 0.25,
                        canvas: true,
                        animate: false,
                        cursor: "grab",
                        touchAction: "none",
                    });
                    canvas.addEventListener("panzoomchange", function (evt) {
                        if (evt && evt.detail && typeof evt.detail.scale === "number") {
                            updateScaleLabel(evt.detail.scale);
                        }
                    });
                    // 若内容在 Panzoom 懒加载完成前已触发 fit/fitWidth，立即用 Panzoom 重套一次避免被默认 100% 覆盖
                    if (contentNaturalSize.width > 0 && contentNaturalSize.height > 0) {
                        if (pendingFitMode === "fit-width") {
                            fitToWidth();
                        } else if (pendingFitMode === "actual") {
                            applyTransform(1.0, 0, 0, false);
                        } else {
                            fitToViewport();
                        }
                    }
                }
            });
        }

        function onWheel(evt) {
            if (isClosed) return;
            evt.preventDefault();
            if (panzoomInstance && typeof panzoomInstance.zoomWithWheel === "function") {
                panzoomInstance.zoomWithWheel(evt, { step: 0.22 });
            } else {
                var delta = evt.deltaY < 0 ? 1.15 : 0.87;
                applyTransform(getCurrentScale() * delta, 0, 0, false);
            }
        }

        function onDblClick(evt) {
            if (evt.target.closest("button, input")) return;
            var cur = getCurrentScale();
            if (Math.abs(cur - lastFitScale) > 0.15) {
                fitToViewport();
            } else {
                var target = Math.min(maxScale, Math.max(2.0, lastFitScale * 2.0));
                applyTransform(target, 0, 0, true);
            }
        }

        var touchStartX = 0;
        var touchStartY = 0;
        var touchTracking = false;
        function onTouchStart(evt) {
            if (!opts.onPrev && !opts.onNext) return;
            if (evt.touches && evt.touches.length === 1 && getCurrentScale() <= lastFitScale + 0.08) {
                touchStartX = evt.touches[0].clientX;
                touchStartY = evt.touches[0].clientY;
                touchTracking = true;
            } else {
                touchTracking = false;
            }
        }
        function onTouchEnd(evt) {
            if (!touchTracking || !evt.changedTouches || !evt.changedTouches.length) return;
            touchTracking = false;
            var dx = evt.changedTouches[0].clientX - touchStartX;
            var dy = evt.changedTouches[0].clientY - touchStartY;
            if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.4) {
                if (dx > 0 && typeof opts.onPrev === "function") {
                    opts.onPrev();
                } else if (dx < 0 && typeof opts.onNext === "function") {
                    opts.onNext();
                }
            }
        }

        function onKeyDown(evt) {
            if (isClosed) return;
            var inInput = evt.target && (evt.target.tagName === "INPUT" || evt.target.tagName === "TEXTAREA");
            if (evt.key === "Escape") {
                evt.preventDefault();
                controller.close();
                return;
            }
            if (inInput) return;
            if (evt.key === "+" || evt.key === "=") {
                evt.preventDefault();
                controller.zoomIn();
            } else if (evt.key === "-" || evt.key === "_") {
                evt.preventDefault();
                controller.zoomOut();
            } else if (evt.key === "0") {
                evt.preventDefault();
                controller.fit();
            } else if (evt.key === "1") {
                evt.preventDefault();
                controller.resetActual();
            } else if (evt.key === "ArrowLeft" && typeof opts.onPrev === "function") {
                evt.preventDefault();
                opts.onPrev();
            } else if (evt.key === "ArrowRight" && typeof opts.onNext === "function") {
                evt.preventDefault();
                opts.onNext();
            }
        }

        viewport.addEventListener("wheel", onWheel, { passive: false });
        viewport.addEventListener("dblclick", onDblClick);
        viewport.addEventListener("touchstart", onTouchStart, { passive: true });
        viewport.addEventListener("touchend", onTouchEnd, { passive: true });
        dialog.addEventListener("keydown", onKeyDown);
        dialog.addEventListener("cancel", function (evt) {
            evt.preventDefault();
            controller.close();
        });

        var controller = {
            dialog: dialog,
            canvas: canvas,
            extraSlot: extraSlot,
            thumbsEl: thumbsEl,
            setTitle: function (text) {
                titleEl.textContent = text || "";
            },
            setMeta: function (text) {
                metaEl.textContent = text || "";
            },
            setCaption: function (text) {
                captionEl.textContent = text || "";
                if (text || !thumbsEl.hidden) {
                    footer.hidden = false;
                }
            },
            setNavVisible: function (visible) {
                prevNavBtn.hidden = !visible;
                nextNavBtn.hidden = !visible;
            },
            setBackdrop: function (mode) {
                var valid = BACKDROP_ORDER.indexOf(mode) !== -1 ? mode : "dark";
                dialog.setAttribute("data-stage-bg", valid);
                var span = btnBackdrop.querySelector("span");
                if (span) span.textContent = BACKDROP_LABELS[valid];
                if (typeof opts.onBackdropChange === "function") {
                    opts.onBackdropChange(valid);
                }
            },
            getBackdrop: function () {
                return dialog.getAttribute("data-stage-bg") || "dark";
            },
            setContent: function (node, naturalSize, mode) {
                while (canvas.firstChild) {
                    canvas.removeChild(canvas.firstChild);
                }
                contentNaturalSize = (naturalSize && naturalSize.width > 0 && naturalSize.height > 0)
                    ? { width: naturalSize.width, height: naturalSize.height }
                    : { width: 0, height: 0 };
                if (node) {
                    canvas.appendChild(node);
                }
                window.requestAnimationFrame(function () {
                    if (isClosed) return;
                    if (mode === "fit-width") {
                        fitToWidth();
                    } else if (mode === "actual") {
                        applyTransform(1.0, 0, 0, false);
                    } else {
                        fitToViewport();
                    }
                });
            },
            updateNaturalSize: function (width, height, refitMode) {
                contentNaturalSize = { width: width || 0, height: height || 0 };
                if (refitMode === "fit-width") {
                    fitToWidth();
                } else if (refitMode === "fit") {
                    fitToViewport();
                }
            },
            fit: fitToViewport,
            fitWidth: fitToWidth,
            resetActual: function () {
                applyTransform(1.0, 0, 0, true);
            },
            zoomIn: function () {
                var cur = getCurrentScale();
                var curPan = panzoomInstance && panzoomInstance.getPan ? panzoomInstance.getPan() : { x: 0, y: 0 };
                applyTransform(cur * 1.25, curPan.x, curPan.y, true);
            },
            zoomOut: function () {
                var cur = getCurrentScale();
                var curPan = panzoomInstance && panzoomInstance.getPan ? panzoomInstance.getPan() : { x: 0, y: 0 };
                applyTransform(cur * 0.8, curPan.x, curPan.y, true);
            },
            getScale: getCurrentScale,
            panToElement: panToElement,
            close: function () {
                if (isClosed) return;
                isClosed = true;
                if (panzoomInstance) {
                    try { panzoomInstance.destroy(); } catch (_e) {}
                    panzoomInstance = null;
                }
                if (dialog.open) {
                    try { dialog.close(); } catch (_e) {}
                }
                dialog.remove();
                if (typeof opts.onClose === "function") {
                    opts.onClose();
                }
                if (restoreFocusEl && typeof restoreFocusEl.focus === "function") {
                    try { restoreFocusEl.focus(); } catch (_e) {}
                }
            },
        };

        closeOtherDialogs(dialog);
        if (typeof dialog.showModal === "function") {
            dialog.showModal();
        } else {
            dialog.setAttribute("open", "open");
        }

        initPanzoomEngine().then(function () {
            if (!isClosed && typeof opts.onReady === "function") {
                opts.onReady(controller);
            }
        });

        return controller;
    }

    window.CBStageViewer = {
        open: openStageViewer,
        createToolbarButton: createToolbarButton,
        makeSvgIcon: makeSvgIcon,
    };
})(window, document);
