/**
 * 全站详情页共享大纲目录交互控制器 (detail-toc.js)
 * 1. 动态测量顶部导航与粘性目录栏高度，写入 CSS 变量，杜绝写死像素导致遮挡标题；
 * 2. 桌面端左侧目录整体折叠/展开 + 二/三/四级分支独立展开收起；
 * 3. 手机/iPad 原生 <dialog> 抽屉打开、关闭与锚点跳转（含顶部导航汉堡菜单自动收起）；
 * 4. 精准阅读线高亮（支持长章节、快速/向上滚动、文末到达、图片加载与自动展开祖先分支）。
 */
(function () {
    "use strict";

    var STORAGE_KEY = "myblog_detail_toc_collapsed";
    var STORAGE_RIGHT_KEY = "myblog_detail_right_collapsed";
    var ATTR_TOC_COLLAPSED = "data-detail-toc-collapsed";
    var ATTR_RIGHT_COLLAPSED = "data-detail-right-collapsed";
    var cachedOcclusion = 82;

    function measureTopOcclusion(mobileBar) {
        var header = document.querySelector(".site-header");
        var headerHeight = 0;
        if (header) {
            var navContainer = header.querySelector(".site-nav-container");
            var barEl = navContainer || header;
            var rect = barEl.getBoundingClientRect();
            if (rect.height > 0 && getComputedStyle(header).position !== "static") {
                headerHeight = Math.ceil(rect.height);
            }
        }

        var mobileBarHeight = 0;
        if (mobileBar && getComputedStyle(mobileBar).display !== "none") {
            var mRect = mobileBar.getBoundingClientRect();
            if (mRect.height > 0) {
                mobileBarHeight = Math.ceil(mRect.height);
            }
        }

        var rootStyle = document.documentElement.style;
        rootStyle.setProperty("--detail-header-height", headerHeight + "px");
        rootStyle.setProperty("--detail-mobile-bar-height", mobileBarHeight + "px");
        cachedOcclusion = headerHeight + mobileBarHeight + 18;
        return cachedOcclusion;
    }

    function initDetailToc() {
        var shell = document.querySelector("[data-detail-shell]");
        if (!shell) {
            return;
        }

        shell.classList.add("js-toc-ready");
        var mobileBar = shell.querySelector(".detail-toc-mobile-bar");
        measureTopOcclusion(mobileBar);

        if ("ResizeObserver" in window) {
            var ro = new window.ResizeObserver(function () {
                measureTopOcclusion(mobileBar);
            });
            var headerEl = document.querySelector(".site-header .site-nav-container") || document.querySelector(".site-header");
            if (headerEl) ro.observe(headerEl);
            if (mobileBar) ro.observe(mobileBar);
        } else {
            window.addEventListener("resize", function () {
                measureTopOcclusion(mobileBar);
            });
        }

        function isNarrowViewport() {
            return window.matchMedia && window.matchMedia("(max-width: 1024px)").matches;
        }

        function navigateToHash(href) {
            if (!href || href.charAt(0) !== "#") return;
            var targetId = href.slice(1);
            var targetEl = document.getElementById(targetId);
            var navCheck = document.getElementById("nav-toggle-check");
            if (navCheck && navCheck.checked) {
                navCheck.checked = false;
            }
            var tocDialog = document.getElementById("detail-toc-dialog");
            var tocOpenBtn = shell.querySelector("[data-toc-open]");
            if (tocDialog && tocDialog.open) {
                tocDialog.close();
                if (tocOpenBtn) tocOpenBtn.setAttribute("aria-expanded", "false");
            }
            if (!targetEl) return;
            var offset = measureTopOcclusion(mobileBar);
            var prefersReduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
            if (targetId === "page-top") {
                window.scrollTo({ top: 0, behavior: prefersReduce ? "auto" : "smooth" });
            } else {
                var targetTop = window.scrollY + targetEl.getBoundingClientRect().top - offset;
                window.scrollTo({ top: Math.max(0, targetTop), behavior: prefersReduce ? "auto" : "smooth" });
            }
            if (window.history && typeof window.history.pushState === "function") {
                try {
                    window.history.pushState(null, "", "#" + targetId);
                } catch (_e) {}
            }
        }

        var quickLinks = Array.prototype.slice.call(shell.querySelectorAll("[data-toc-quick]"));
        quickLinks.forEach(function (link) {
            link.addEventListener("click", function (evt) {
                var href = link.getAttribute("href") || "";
                if (href.charAt(0) === "#") {
                    evt.preventDefault();
                    navigateToHash(href);
                }
            });
        });

        // A. 右侧信息栏可折叠与窄屏快捷入口（独立初始化，兼容无目录页）
        if (shell.classList.contains("has-right")) {
            var rightCollapseBtn = shell.querySelector("[data-right-collapse]");
            var rightAside = document.getElementById("detail-right-aside");
            var rightMobileJump = shell.querySelector("[data-right-mobile-jump]");

            function syncRightUi() {
                var narrow = isNarrowViewport();
                var collapsed = narrow
                    ? shell.classList.contains("is-mobile-right-collapsed")
                    : shell.classList.contains("is-right-collapsed");
                if (rightCollapseBtn) {
                    rightCollapseBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
                    var label = collapsed ? "展开右侧信息栏" : "收起右侧信息栏";
                    rightCollapseBtn.setAttribute("title", label);
                    rightCollapseBtn.setAttribute("aria-label", label);
                }
            }

            function setDesktopRightCollapsed(collapsed, persist) {
                shell.classList.toggle("is-right-collapsed", collapsed);
                if (collapsed) {
                    document.documentElement.setAttribute(ATTR_RIGHT_COLLAPSED, "1");
                } else {
                    document.documentElement.removeAttribute(ATTR_RIGHT_COLLAPSED);
                }
                if (persist) {
                    try {
                        localStorage.setItem(STORAGE_RIGHT_KEY, collapsed ? "1" : "0");
                    } catch (_e) {}
                }
                syncRightUi();
            }

            // 桌面端恢复偏好；窄屏默认收起为单行条避免把评论顶到几屏之后
            var initialDesktopCollapsed = false;
            try {
                initialDesktopCollapsed = localStorage.getItem(STORAGE_RIGHT_KEY) === "1";
            } catch (_e) {}
            setDesktopRightCollapsed(initialDesktopCollapsed, false);
            shell.classList.add("is-mobile-right-collapsed");
            syncRightUi();

            if (rightCollapseBtn) {
                rightCollapseBtn.addEventListener("click", function () {
                    if (isNarrowViewport()) {
                        shell.classList.toggle("is-mobile-right-collapsed");
                        syncRightUi();
                    } else {
                        setDesktopRightCollapsed(!shell.classList.contains("is-right-collapsed"), true);
                    }
                });
            }

            if (rightMobileJump && rightAside) {
                rightMobileJump.addEventListener("click", function () {
                    shell.classList.remove("is-mobile-right-collapsed");
                    syncRightUi();
                    var navCheck = document.getElementById("nav-toggle-check");
                    if (navCheck && navCheck.checked) {
                        navCheck.checked = false;
                    }
                    var offset = measureTopOcclusion(mobileBar);
                    var prefersReduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
                    var targetTop = window.scrollY + rightAside.getBoundingClientRect().top - offset;
                    window.scrollTo({ top: Math.max(0, targetTop), behavior: prefersReduce ? "auto" : "smooth" });
                });
            }

            window.addEventListener("resize", syncRightUi, { passive: true });
        }

        // B. 左侧目录大纲控制器
        if (!shell.classList.contains("has-toc")) {
            return;
        }

        var collapseBtn = shell.querySelector("[data-toc-collapse]");
        var openBtn = shell.querySelector("[data-toc-open]");
        var dialog = document.getElementById("detail-toc-dialog");
        var closeBtn = dialog ? dialog.querySelector("[data-toc-close]") : null;
        var tocLinks = Array.prototype.slice.call(shell.querySelectorAll("[data-toc-link]"));
        var branchToggles = Array.prototype.slice.call(shell.querySelectorAll("[data-toc-branch-toggle]"));

        // 1. 桌面折叠状态恢复与切换
        function setCollapsed(collapsed) {
            shell.classList.toggle("is-toc-collapsed", collapsed);
            if (collapsed) {
                document.documentElement.setAttribute(ATTR_TOC_COLLAPSED, "1");
            } else {
                document.documentElement.removeAttribute(ATTR_TOC_COLLAPSED);
            }
            if (collapseBtn) {
                collapseBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
                collapseBtn.setAttribute("title", collapsed ? "展开目录" : "收起目录");
                collapseBtn.setAttribute("aria-label", collapsed ? "展开目录" : "收起目录");
            }
        }

        try {
            if (localStorage.getItem(STORAGE_KEY) === "1") {
                setCollapsed(true);
            }
        } catch (_e) {}

        if (collapseBtn) {
            collapseBtn.addEventListener("click", function () {
                var next = !shell.classList.contains("is-toc-collapsed");
                setCollapsed(next);
                try {
                    localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
                } catch (_e) {}
            });
        }

        // 1.5 二/三/四级分支折叠与展开
        function setBranchExpanded(itemEl, expanded) {
            if (!itemEl) return;
            var btn = itemEl.querySelector(":scope > .detail-toc-row > [data-toc-branch-toggle]");
            var sub = itemEl.querySelector(":scope > [data-toc-sublist]");
            if (btn && sub) {
                btn.setAttribute("aria-expanded", expanded ? "true" : "false");
                sub.hidden = !expanded;
            }
        }

        branchToggles.forEach(function (btn) {
            btn.addEventListener("click", function (evt) {
                evt.preventDefault();
                evt.stopPropagation();
                var itemEl = btn.closest("[data-toc-node]");
                if (!itemEl) return;
                var isExp = btn.getAttribute("aria-expanded") !== "false";
                setBranchExpanded(itemEl, !isExp);
            });
        });

        // 2. 手机/iPad <dialog> 抽屉控制
        function closeDrawer() {
            if (dialog && dialog.open) {
                dialog.close();
            }
            if (openBtn) {
                openBtn.setAttribute("aria-expanded", "false");
            }
        }

        if (openBtn && dialog && typeof dialog.showModal === "function") {
            openBtn.addEventListener("click", function () {
                if (!dialog.open) {
                    document.querySelectorAll("dialog[open]").forEach(function (other) {
                        if (other === dialog) return;
                        if (typeof other.close === "function") {
                            other.close();
                        } else {
                            other.removeAttribute("open");
                        }
                        if (other.id) {
                            document.querySelectorAll('[aria-controls="' + other.id + '"]').forEach(function (ctrl) {
                                ctrl.setAttribute("aria-expanded", "false");
                            });
                        }
                    });
                    var navCheck = document.getElementById("nav-toggle-check");
                    if (navCheck && navCheck.checked) {
                        navCheck.checked = false;
                    }
                    dialog.showModal();
                    openBtn.setAttribute("aria-expanded", "true");
                }
            });
        }

        if (closeBtn) {
            closeBtn.addEventListener("click", closeDrawer);
        }

        if (dialog) {
            dialog.addEventListener("click", function (evt) {
                if (evt.target === dialog) {
                    closeDrawer();
                }
            });
            dialog.addEventListener("close", function () {
                if (openBtn) {
                    openBtn.setAttribute("aria-expanded", "false");
                }
            });
        }

        // 点击目录锚点：关闭抽屉/菜单后精准定位
        tocLinks.forEach(function (link) {
            link.addEventListener("click", function (evt) {
                var href = link.getAttribute("href") || "";
                if (href.charAt(0) === "#") {
                    evt.preventDefault();
                    navigateToHash(href);
                }
            });
        });

        // 3. 当前章节高亮联动 (Scroll Spy)
        var targetIds = [];
        tocLinks.forEach(function (link) {
            var href = link.getAttribute("href") || "";
            if (href.charAt(0) === "#") {
                var id = href.slice(1);
                if (targetIds.indexOf(id) === -1) {
                    targetIds.push(id);
                }
            }
        });

        var targets = targetIds
            .map(function (id) { return document.getElementById(id); })
            .filter(Boolean);

        function expandAncestorsForLink(link) {
            var parent = link.closest("[data-toc-node]");
            while (parent) {
                var parentNode = parent.parentElement ? parent.parentElement.closest("[data-toc-node]") : null;
                if (parentNode) {
                    setBranchExpanded(parentNode, true);
                }
                parent = parentNode;
            }
        }

        function setActiveLink(activeId) {
            tocLinks.forEach(function (link) {
                var match = activeId && link.getAttribute("href") === "#" + activeId;
                link.classList.toggle("is-active", !!match);
                if (match) {
                    link.setAttribute("aria-current", "location");
                    expandAncestorsForLink(link);
                } else {
                    link.removeAttribute("aria-current");
                }
            });
        }

        function updateActiveFromScroll() {
            if (!targets.length) return;
            var readingLine = cachedOcclusion + 24;
            var activeId = "";

            for (var i = 0; i < targets.length; i++) {
                var rect = targets[i].getBoundingClientRect();
                if (rect.top <= readingLine) {
                    activeId = targets[i].id;
                } else {
                    break;
                }
            }

            var nearBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 16;
            if (nearBottom && targets.length) {
                var lastEl = targets[targets.length - 1];
                if (lastEl.getBoundingClientRect().top < window.innerHeight) {
                    activeId = lastEl.id;
                }
            }

            setActiveLink(activeId);
        }

        if (targets.length) {
            var rafPending = false;
            var scheduleUpdate = function () {
                if (rafPending) return;
                rafPending = true;
                window.requestAnimationFrame(function () {
                    rafPending = false;
                    updateActiveFromScroll();
                });
            };
            window.addEventListener("scroll", scheduleUpdate, { passive: true });
            window.addEventListener("resize", scheduleUpdate, { passive: true });
            window.addEventListener("load", updateActiveFromScroll);
            updateActiveFromScroll();
        }
    }

    document.addEventListener("DOMContentLoaded", initDetailToc);
})();
