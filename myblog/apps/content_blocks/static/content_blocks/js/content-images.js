/**
 * 全站公共图片灯箱放大引擎 (content-images.js)
 * 统一接管正文插图 (.media-image-wrapper / 文章正文图片) 与文章独立图集 (.gallery-card)，
 * 支持 Esc 退出、左右方向键切图与焦点记忆恢复。
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
            items.push({
                src: src,
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
                caption: (capEl ? capEl.textContent : img.alt) || "",
                trigger: img
            });
        });

        if (items.length === 0) return;
        if (document.querySelector(".cb-lightbox-overlay")) return;

        var overlay = document.createElement("div");
        overlay.className = "cb-lightbox-overlay";
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", "图片灯箱查看器");
        overlay.tabIndex = -1;
        overlay.innerHTML =
            '<button type="button" class="cb-lightbox-btn cb-lightbox-close" aria-label="关闭灯箱 (Esc)">×</button>' +
            '<button type="button" class="cb-lightbox-btn cb-lightbox-prev" aria-label="上一张 (←)">‹</button>' +
            '<img class="cb-lightbox-img" src="" alt="大图预览" />' +
            '<button type="button" class="cb-lightbox-btn cb-lightbox-next" aria-label="下一张 (→)">›</button>' +
            '<div class="cb-lightbox-caption"></div>';
        document.body.appendChild(overlay);

        var imgEl = overlay.querySelector(".cb-lightbox-img");
        var capEl = overlay.querySelector(".cb-lightbox-caption");
        var closeBtn = overlay.querySelector(".cb-lightbox-close");
        var prevBtn = overlay.querySelector(".cb-lightbox-prev");
        var nextBtn = overlay.querySelector(".cb-lightbox-next");

        var currentIdx = 0;
        var lastTrigger = null;

        function show(idx) {
            if (idx < 0) idx = items.length - 1;
            if (idx >= items.length) idx = 0;
            currentIdx = idx;
            var it = items[currentIdx];
            imgEl.src = it.src;
            imgEl.alt = it.caption || ("图片 " + (currentIdx + 1));
            capEl.textContent = it.caption
                ? (it.caption + " (" + (currentIdx + 1) + "/" + items.length + ")")
                : ("(" + (currentIdx + 1) + "/" + items.length + ")");
            prevBtn.hidden = items.length <= 1;
            nextBtn.hidden = items.length <= 1;
        }

        function open(idx, trigger) {
            lastTrigger = trigger;
            show(idx);
            overlay.classList.add("is-active");
            overlay.focus();
            document.body.style.overflow = "hidden";
        }

        function close() {
            overlay.classList.remove("is-active");
            document.body.style.overflow = "";
            if (lastTrigger && typeof lastTrigger.focus === "function") {
                lastTrigger.focus();
            }
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
                open(idx, el);
            });
            el.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    open(idx, el);
                }
            });
        });

        closeBtn.addEventListener("click", close);
        prevBtn.addEventListener("click", function () { show(currentIdx - 1); });
        nextBtn.addEventListener("click", function () { show(currentIdx + 1); });
        overlay.addEventListener("click", function (e) {
            if (e.target === overlay) close();
        });
        window.addEventListener("keydown", function (e) {
            if (!overlay.classList.contains("is-active")) return;
            if (e.key === "Escape") {
                e.preventDefault();
                close();
            } else if (e.key === "ArrowLeft") {
                e.preventDefault();
                show(currentIdx - 1);
            } else if (e.key === "ArrowRight") {
                e.preventDefault();
                show(currentIdx + 1);
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initContentImageLightbox);
    } else {
        initContentImageLightbox();
    }

    window.initContentImageLightbox = initContentImageLightbox;
})(window, document);
