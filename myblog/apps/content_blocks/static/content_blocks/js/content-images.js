/**
 * 全站公共图片灯箱放大引擎 (content-images.js)
 * 统一接管正文插图 (.media-image-wrapper / 文章正文图片) 与文章独立图集 (.gallery-card)，
 * 依托共享 CBStageViewer 提供定点滚轮缩放、拖拽平移、长图适宽、1:1 原尺寸、90° 旋转、
 * 暗/浅/棋盘格底色切换、底部多图缩略图条、分辨率信息与键盘/手势导航（不提供显式下载按钮）。
 */

(function (window, document) {
    "use strict";

    function initContentImageLightbox() {
        var galleryCards = Array.from(document.querySelectorAll(".gallery-card"));
        var bodyImages = Array.from(
            document.querySelectorAll(
                ".media-image-wrapper img, .detail-body-section img, .blog-body-section img, .guide-main img, .project-body-section img"
            )
        ).filter(function (img) {
            if (img.closest(".gallery-card")) return false;
            if (img.closest(".cb-mermaid")) return false;
            if (img.classList.contains("author-avatar")) return false;
            return true;
        });

        var items = [];
        galleryCards.forEach(function (card) {
            var src = card.getAttribute("data-full-src") || (card.querySelector("img") ? card.querySelector("img").src : "");
            if (!src) return;
            var thumbEl = card.querySelector("img");
            items.push({
                src: src,
                thumbSrc: (thumbEl && thumbEl.src) || src,
                caption: card.getAttribute("data-caption") || "",
                trigger: card
            });
        });

        bodyImages.forEach(function (img) {
            var fullSrc = img.getAttribute("data-full-src") || img.src;
            if (!fullSrc) return;
            var wrapper = img.closest("figure");
            var capEl = wrapper ? wrapper.querySelector("figcaption") : null;
            items.push({
                src: fullSrc,
                thumbSrc: img.src || fullSrc,
                caption: (capEl ? capEl.textContent : img.alt) || "",
                trigger: img
            });
        });

        if (items.length === 0) return;
        if (document.documentElement.dataset.cbImagesBound === "1") return;
        document.documentElement.dataset.cbImagesBound = "1";

        var currentIdx = 0;
        var rotationDeg = 0;
        var activeViewer = null;
        var activeImg = null;
        var activeWrap = null;
        var thumbButtons = [];

        var ROTATE_LEFT_SVG = '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>';
        var ROTATE_RIGHT_SVG = '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>';

        function effectiveDimensions(imgEl, deg) {
            var nw = imgEl.naturalWidth || imgEl.width || 0;
            var nh = imgEl.naturalHeight || imgEl.height || 0;
            if (!nw || !nh) return { width: 0, height: 0 };
            var normalized = ((deg % 360) + 360) % 360;
            if (normalized === 90 || normalized === 270) {
                return { width: nh, height: nw };
            }
            return { width: nw, height: nh };
        }

        function updateImageMeta(imgEl, item) {
            if (!activeViewer) return;
            var nw = imgEl.naturalWidth || 0;
            var nh = imgEl.naturalHeight || 0;
            var idxText = (currentIdx + 1) + " / " + items.length;
            var resText = (nw > 0 && nh > 0) ? (nw + " × " + nh) : "";
            activeViewer.setTitle(item.caption || "图片预览");
            activeViewer.setMeta(resText ? (idxText + " · " + resText) : idxText);
            activeViewer.setCaption(item.caption || ("第 " + (currentIdx + 1) + " 张，共 " + items.length + " 张"));
        }

        function applyRotation(nextDeg) {
            rotationDeg = ((nextDeg % 360) + 360) % 360;
            if (activeWrap) {
                activeWrap.style.transform = rotationDeg ? ("rotate(" + rotationDeg + "deg)") : "";
            }
            if (activeViewer && activeImg) {
                var dims = effectiveDimensions(activeImg, rotationDeg);
                activeViewer.updateNaturalSize(dims.width, dims.height, "fit");
            }
        }

        function renderCurrentItem(idx) {
            if (!activeViewer) return;
            if (idx < 0) idx = items.length - 1;
            if (idx >= items.length) idx = 0;
            currentIdx = idx;
            rotationDeg = 0;

            var item = items[currentIdx];
            var wrap = document.createElement("div");
            wrap.className = "cb-stage-img-wrap";
            var img = document.createElement("img");
            img.className = "cb-stage-img";
            img.alt = item.caption || ("图片 " + (currentIdx + 1));
            img.draggable = false;
            wrap.appendChild(img);

            activeWrap = wrap;
            activeImg = img;

            thumbButtons.forEach(function (btn, i) {
                btn.classList.toggle("is-active", i === currentIdx);
                btn.setAttribute("aria-current", i === currentIdx ? "true" : "false");
                if (i === currentIdx && typeof btn.scrollIntoView === "function") {
                    btn.scrollIntoView({ inline: "nearest", block: "nearest" });
                }
            });

            updateImageMeta(img, item);
            activeViewer.setContent(wrap, { width: 0, height: 0 }, "fit");

            img.onload = function () {
                if (activeImg !== img || !activeViewer) return;
                updateImageMeta(img, item);
                var dims = effectiveDimensions(img, rotationDeg);
                // 长图自动识别（高度明显大于宽度的纵向长图），默认走适配宽度顶对齐阅读
                var isTallLongImage = dims.width > 0 && dims.height > dims.width * 1.85;
                activeViewer.updateNaturalSize(dims.width, dims.height, isTallLongImage ? "fit-width" : "fit");
            };
            img.src = item.src;
        }

        function openLightbox(idx, trigger) {
            if (!window.CBStageViewer || typeof window.CBStageViewer.open !== "function") {
                // 兜底：公共查看器未就绪时仅新标签打开受控网关地址
                if (items[idx] && items[idx].src) {
                    window.open(items[idx].src, "_blank", "noopener");
                }
                return;
            }

            thumbButtons = [];
            activeViewer = window.CBStageViewer.open({
                title: "图片预览",
                ariaLabel: "图片灯箱查看器",
                minScale: 0.25,
                maxScale: 5.0,
                maxFitScale: 1.0,
                enableFitWidth: true,
                initialBackdrop: "dark",
                showFooter: true,
                hintText: items.length > 1
                    ? "滚轮缩放 · 拖拽平移 · ←/→ 切图 · 双击切换 · Esc 退出"
                    : "滚轮缩放 · 拖拽平移 · 双击切换 · Esc 退出",
                triggerEl: trigger,
                onPrev: items.length > 1 ? function () { renderCurrentItem(currentIdx - 1); } : null,
                onNext: items.length > 1 ? function () { renderCurrentItem(currentIdx + 1); } : null,
                onClose: function () {
                    activeViewer = null;
                    activeImg = null;
                    activeWrap = null;
                    thumbButtons = [];
                },
            });

            activeViewer.setNavVisible(items.length > 1);

            // 图片专属控制：90° 左旋 / 右旋（按决策 A 不提供显式下载按钮）
            var btnRotateLeft = window.CBStageViewer.createToolbarButton({
                icon: ROTATE_LEFT_SVG,
                title: "向左旋转 90°",
                ariaLabel: "向左旋转 90 度",
                onClick: function () { applyRotation(rotationDeg - 90); },
            });
            var btnRotateRight = window.CBStageViewer.createToolbarButton({
                icon: ROTATE_RIGHT_SVG,
                title: "向右旋转 90°",
                ariaLabel: "向右旋转 90 度",
                onClick: function () { applyRotation(rotationDeg + 90); },
            });
            activeViewer.extraSlot.appendChild(btnRotateLeft);
            activeViewer.extraSlot.appendChild(btnRotateRight);

            // 多图底部缩略图导览条
            if (items.length >= 2) {
                activeViewer.thumbsEl.hidden = false;
                items.forEach(function (it, i) {
                    var tb = document.createElement("button");
                    tb.type = "button";
                    tb.className = "cb-stage-thumb-btn";
                    tb.title = it.caption || ("第 " + (i + 1) + " 张");
                    tb.setAttribute("aria-label", "切换到第 " + (i + 1) + " 张图片");
                    var tImg = document.createElement("img");
                    tImg.src = it.thumbSrc || it.src;
                    tImg.alt = "";
                    tImg.loading = "lazy";
                    tb.appendChild(tImg);
                    tb.addEventListener("click", function () {
                        renderCurrentItem(i);
                    });
                    activeViewer.thumbsEl.appendChild(tb);
                    thumbButtons.push(tb);
                });
            }

            renderCurrentItem(idx);
        }

        items.forEach(function (item, idx) {
            var el = item.trigger;
            el.style.cursor = "zoom-in";
            if (el.tagName !== "A") {
                el.tabIndex = 0;
                el.setAttribute("role", "button");
            }
            el.addEventListener("click", function (e) {
                if (el.closest("a")) e.preventDefault();
                openLightbox(idx, el);
            });
            el.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    openLightbox(idx, el);
                }
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initContentImageLightbox);
    } else {
        initContentImageLightbox();
    }

    window.initContentImageLightbox = initContentImageLightbox;
})(window, document);
