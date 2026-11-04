/**
 * S10 文章详情页交互增强脚本 (article.js)
 * 1. 简介渐进增强折叠：长简介自动添加展开/收起按钮与 aria-expanded 状态；
 * 2. 代码块一键复制；
 * 3. 独立图集 Lightbox 灯箱：支持键盘导航 (Esc 退出、左右箭头切图、焦点记忆恢复)。
 */

document.addEventListener("DOMContentLoaded", function () {
    // =========================================================================
    // 1. 简介渐进增强折叠
    // =========================================================================
    const introCard = document.querySelector(".blog-intro-card");
    if (introCard) {
        const introContent = introCard.querySelector(".intro-content");
        if (introContent && introContent.scrollHeight > 160) {
            introCard.classList.add("has-toggle");
            introContent.classList.add("is-collapsed");

            const toggleBtn = document.createElement("button");
            toggleBtn.type = "button";
            toggleBtn.className = "intro-toggle-btn";
            toggleBtn.setAttribute("aria-expanded", "false");
            toggleBtn.textContent = "展开阅读完整简介 ▼";

            toggleBtn.addEventListener("click", function () {
                const isCollapsed = introContent.classList.contains("is-collapsed");
                if (isCollapsed) {
                    introContent.classList.remove("is-collapsed");
                    toggleBtn.setAttribute("aria-expanded", "true");
                    toggleBtn.textContent = "收起简介 ▲";
                } else {
                    introContent.classList.add("is-collapsed");
                    toggleBtn.setAttribute("aria-expanded", "false");
                    toggleBtn.textContent = "展开阅读完整简介 ▼";
                    introCard.scrollIntoView({ behavior: "smooth", block: "start" });
                }
            });

            introCard.appendChild(toggleBtn);
        }
    }

    // =========================================================================
    // 2. 代码块一键复制
    // =========================================================================
    document.querySelectorAll(".copy-code-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            const encodedCode = btn.getAttribute("data-clipboard-text");
            if (!encodedCode) return;
            const rawCode = decodeURIComponent(encodedCode);

            navigator.clipboard.writeText(rawCode).then(function () {
                const originalText = btn.textContent;
                btn.textContent = "已复制 ✓";
                btn.style.borderColor = "#4ade80";
                btn.style.color = "#4ade80";
                setTimeout(function () {
                    btn.textContent = originalText;
                    btn.style.borderColor = "";
                    btn.style.color = "";
                }, 2000);
            }).catch(function () {
                // 回退剪贴板策略
                const textarea = document.createElement("textarea");
                textarea.value = rawCode;
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand("copy");
                document.body.removeChild(textarea);
                btn.textContent = "已复制 ✓";
                setTimeout(() => { btn.textContent = "复制"; }, 2000);
            });
        });
    });

    // =========================================================================
    // 3. 独立图集 Lightbox 键盘灯箱
    // =========================================================================
    const galleryCards = document.querySelectorAll(".gallery-card");
    if (galleryCards.length > 0) {
        // 构建灯箱容器 DOM
        const overlay = document.createElement("div");
        overlay.className = "lightbox-overlay";
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", "图片画廊灯箱");
        overlay.tabIndex = -1;

        overlay.innerHTML = `
            <button type="button" class="lightbox-btn lightbox-close" aria-label="关闭灯箱 (Esc)">×</button>
            <button type="button" class="lightbox-btn lightbox-prev" aria-label="上一张图片 (←)">‹</button>
            <img class="lightbox-img" src="" alt="灯箱大图" />
            <button type="button" class="lightbox-btn lightbox-next" aria-label="下一张图片 (→)">›</button>
            <div class="lightbox-caption"></div>
        `;
        document.body.appendChild(overlay);

        const lightboxImg = overlay.querySelector(".lightbox-img");
        const lightboxCaption = overlay.querySelector(".lightbox-caption");
        const closeBtn = overlay.querySelector(".lightbox-close");
        const prevBtn = overlay.querySelector(".lightbox-prev");
        const nextBtn = overlay.querySelector(".lightbox-next");

        const itemsData = Array.from(galleryCards).map(function (card) {
            return {
                src: card.getAttribute("data-full-src") || card.querySelector("img")?.src || "",
                caption: card.getAttribute("data-caption") || "",
                triggerElement: card,
            };
        });

        let currentIndex = 0;
        let lastFocusedElement = null;

        function showImage(index) {
            if (index < 0) index = itemsData.length - 1;
            if (index >= itemsData.length) index = 0;
            currentIndex = index;

            const item = itemsData[currentIndex];
            lightboxImg.src = item.src;
            lightboxImg.alt = item.caption || `图片 ${currentIndex + 1}`;
            lightboxCaption.textContent = item.caption ? `${item.caption} (${currentIndex + 1}/${itemsData.length})` : `(${currentIndex + 1}/${itemsData.length})`;
        }

        function openLightbox(index, triggerEl) {
            lastFocusedElement = triggerEl;
            showImage(index);
            overlay.classList.add("is-active");
            overlay.focus();
            document.body.style.overflow = "hidden";
        }

        function closeLightbox() {
            overlay.classList.remove("is-active");
            document.body.style.overflow = "";
            if (lastFocusedElement) {
                lastFocusedElement.focus();
            }
        }

        galleryCards.forEach(function (card, idx) {
            card.tabIndex = 0;
            card.setAttribute("role", "button");
            card.setAttribute("aria-label", `点击放大查看图片: ${card.getAttribute("data-caption") || "画廊图片"}`);

            card.addEventListener("click", function () {
                openLightbox(idx, card);
            });
            card.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    openLightbox(idx, card);
                }
            });
        });

        closeBtn.addEventListener("click", closeLightbox);
        prevBtn.addEventListener("click", function () { showImage(currentIndex - 1); });
        nextBtn.addEventListener("click", function () { showImage(currentIndex + 1); });

        // 点击遮罩空白区域退出
        overlay.addEventListener("click", function (e) {
            if (e.target === overlay) {
                closeLightbox();
            }
        });

        // 键盘无障碍控制 (Esc, ←, →)
        window.addEventListener("keydown", function (e) {
            if (!overlay.classList.contains("is-active")) return;
            if (e.key === "Escape") {
                e.preventDefault();
                closeLightbox();
            } else if (e.key === "ArrowLeft") {
                e.preventDefault();
                showImage(currentIndex - 1);
            } else if (e.key === "ArrowRight") {
                e.preventDefault();
                showImage(currentIndex + 1);
            }
        });
    }
});
