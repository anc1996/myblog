/**
 * 全站公共 UI 组件统一挂载桥接器 (MyblogUI)
 *
 * 涵盖：
 * 1. 浅/深色主题切换（持久化至 localStorage 并同步 root[data-theme]）；
 * 2. Tom Select ([data-ui-select])：可搜索下拉框与多选标签胶囊；
 * 3. Flatpickr ([data-ui-date])：统一桌面/iPad/手机的中文日期选择框；
 * 4. Splide ([data-ui-carousel])：自适应响应式触控轮播图；
 * 5. HTMX CSRF 自动注入与局部刷新自动重新挂载。
 */
(function () {
    "use strict";

    var THEME_KEY = "myblog_theme_preference";

    function getPreferredTheme() {
        try {
            var saved = localStorage.getItem(THEME_KEY);
            if (saved === "light" || saved === "dark") {
                return saved;
            }
        } catch (_e) {}
        if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
            return "dark";
        }
        return "light";
    }

    function applyTheme(theme) {
        var val = theme === "dark" ? "dark" : "light";
        document.documentElement.setAttribute("data-theme", val);
        var btn = document.querySelector("[data-theme-toggle]");
        if (btn) {
            btn.textContent = val === "dark" ? "☀️" : "🌙";
            btn.setAttribute("aria-label", val === "dark" ? "切换到浅色主题" : "切换到深色主题");
            btn.setAttribute("title", val === "dark" ? "切换到浅色主题" : "切换到深色主题");
        }
    }

    function initThemeToggle() {
        applyTheme(getPreferredTheme());
        var btn = document.querySelector("[data-theme-toggle]");
        if (!btn || btn.dataset.uiBound === "1") return;
        btn.dataset.uiBound = "1";
        btn.addEventListener("click", function () {
            var current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
            var next = current === "dark" ? "light" : "dark";
            try {
                localStorage.setItem(THEME_KEY, next);
            } catch (_e) {}
            applyTheme(next);
        });
    }

    function mountSelects(root) {
        if (typeof window.TomSelect !== "function") return;
        var nodes = (root || document).querySelectorAll("select[data-ui-select]");
        nodes.forEach(function (el) {
            if (el.tomselect || el.dataset.uiMounted === "1") return;
            el.dataset.uiMounted = "1";
            var allowClear = el.getAttribute("data-ui-clear") === "true";
            var placeholder = el.getAttribute("data-placeholder") || "输入关键词搜索筛选...";
            var remoteUrl = el.getAttribute("data-ui-select-remote") || "";
            var plugins = [];
            if (el.multiple) plugins.push("remove_button");
            if (allowClear) plugins.push("clear_button");
            var config = {
                plugins: plugins,
                placeholder: placeholder,
                allowEmptyOption: true,
                maxOptions: 200,
                onChange: function () {
                    el.dispatchEvent(new Event("change", { bubbles: true }));
                },
            };
            if (remoteUrl) {
                config.valueField = "value";
                config.labelField = "label";
                config.searchField = ["label"];
                config.preload = "focus";
                config.load = function (query, callback) {
                    var sep = remoteUrl.indexOf("?") === -1 ? "?" : "&";
                    fetch(remoteUrl + sep + "q=" + encodeURIComponent(query || ""), {
                        headers: { "X-Requested-With": "XMLHttpRequest" },
                    })
                        .then(function (resp) { return resp.ok ? resp.json() : { items: [] }; })
                        .then(function (data) { callback((data && Array.isArray(data.items)) ? data.items : []); })
                        .catch(function () { callback(); });
                };
            }
            new window.TomSelect(el, config);
        });
    }

    function mountDates(root) {
        if (typeof window.flatpickr !== "function") return;
        var nodes = (root || document).querySelectorAll("input[data-ui-date]");
        var zhLocale = (window.flatpickr.l10ns && window.flatpickr.l10ns.zh) ? window.flatpickr.l10ns.zh : "default";
        nodes.forEach(function (el) {
            if (el._flatpickr || el.dataset.uiMounted === "1") return;
            el.dataset.uiMounted = "1";
            var mode = el.getAttribute("data-ui-date") || "single";
            window.flatpickr(el, {
                locale: zhLocale,
                dateFormat: "Y-m-d",
                mode: mode === "range" ? "range" : "single",
                allowInput: true,
                disableMobile: true,
                onChange: function () {
                    el.dispatchEvent(new Event("change", { bubbles: true }));
                },
            });
        });
    }

    function mountCarousels(root) {
        if (typeof window.Splide !== "function") return;
        var nodes = (root || document).querySelectorAll("[data-ui-carousel]");
        nodes.forEach(function (el) {
            if (el.dataset.uiMounted === "1" || el.classList.contains("is-initialized")) return;
            el.dataset.uiMounted = "1";
            var perPage = parseInt(el.getAttribute("data-per-page") || "3", 10) || 3;
            var autoplay = el.getAttribute("data-autoplay") === "true";
            var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
            var splide = new window.Splide(el, {
                type: el.querySelectorAll(".splide__slide").length > perPage ? "loop" : "slide",
                perPage: perPage,
                perMove: 1,
                gap: "16px",
                autoplay: autoplay && !reduceMotion,
                pauseOnHover: true,
                pauseOnFocus: true,
                pagination: true,
                arrows: true,
                breakpoints: {
                    1024: { perPage: Math.min(2, perPage) },
                    640: { perPage: 1 },
                },
            });
            splide.mount();
        });
    }

    function getCsrfToken() {
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function initHtmxCsrf() {
        document.body.addEventListener("htmx:configRequest", function (evt) {
            var token = getCsrfToken();
            if (token && evt.detail && evt.detail.headers) {
                evt.detail.headers["X-CSRFToken"] = token;
            }
        });
        document.body.addEventListener("htmx:afterSwap", function (evt) {
            mountAll(evt.detail ? evt.detail.target : document);
        });
    }

    function mountAll(root) {
        var scope = root || document;
        mountSelects(scope);
        mountDates(scope);
        mountCarousels(scope);
    }

    window.MyblogUI = {
        mount: mountAll,
        applyTheme: applyTheme,
    };

    document.addEventListener("DOMContentLoaded", function () {
        initThemeToggle();
        initHtmxCsrf();
        mountAll(document);
    });
})();
