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
    // 2. 代码块一键复制 (委托公共模块 code-copy.js 执行)
    // =========================================================================
    if (window.initCodeCopy) {
        window.initCodeCopy();
    }

    // =========================================================================
    // 3. 独立图集 Lightbox 键盘灯箱 (委托公共模块 content-images.js 执行)
    // =========================================================================
    if (window.initContentImageLightbox) {
        window.initContentImageLightbox();
    }
});
