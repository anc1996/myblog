/**
 * 全站公共 UI 组件统一挂载桥接器 (MyblogUI)
 *
 * 涵盖：
 * 1. 三态昼夜主题控制器（复用 base.html 注入的 window.__myblogTheme 单源状态机）；
 * 2. Tom Select ([data-ui-select])：可搜索下拉框与多选标签胶囊；
 * 3. Flatpickr ([data-ui-date])：统一桌面/iPad/手机的中文日期选择框；
 * 4. Splide ([data-ui-carousel])：自适应响应式触控轮播图；
 * 5. HTMX CSRF 自动注入与局部刷新自动重新挂载。
 */
(function () {
    "use strict";

    var globalThemeListenersBound = false;
    var MODE_META = {
        system: { icon: "🌓", shortLabel: "系统", fullLabel: "跟随系统" },
        light: { icon: "☀️", shortLabel: "白天", fullLabel: "白天（浅色）" },
        dark: { icon: "🌙", shortLabel: "黑夜", fullLabel: "黑夜（深色）" },
    };

    function getThemeApi() {
        if (window.__myblogTheme) {
            return window.__myblogTheme;
        }
        // 极简兜底：仅用于脱离 base.html 的孤立测试页
        var root = document.documentElement;
        return {
            STORAGE_KEY: "myblog_theme_preference",
            getState: function () {
                var t = root.getAttribute("data-theme") === "dark" ? "dark" : "light";
                var m = root.getAttribute("data-theme-mode") || "system";
                return {
                    theme: t,
                    mode: m,
                    palette: root.getAttribute("data-palette") || "mist-blue",
                    hasOverride: m !== "system",
                    previewChoice: "site",
                    inPreview: root.getAttribute("data-in-preview") === "1",
                };
            },
            sync: function () {
                return this.getState();
            },
            setMode: function (next) {
                var resolved = next === "dark" ? "dark" : "light";
                if (next === "system" || next === "reset") {
                    resolved = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
                }
                root.setAttribute("data-theme", resolved);
                root.setAttribute("data-theme-mode", next === "reset" ? "system" : (next || "system"));
                root.style.colorScheme = resolved;
                return this.getState();
            },
            reset: function () {
                return this.setMode("reset");
            },
        };
    }

    function syncThemeUI(state) {
        var st = state || getThemeApi().getState();
        var meta = MODE_META[st.mode] || MODE_META.system;
        var resolvedText = st.theme === "dark" ? "深色" : "浅色";
        var toggleBtn = document.querySelector("[data-theme-toggle]");
        if (toggleBtn) {
            var iconEl = toggleBtn.querySelector("[data-theme-icon]");
            var labelEl = toggleBtn.querySelector("[data-theme-label]");
            if (iconEl) iconEl.textContent = meta.icon;
            if (labelEl) labelEl.textContent = meta.shortLabel;
            if (!iconEl && !labelEl) toggleBtn.textContent = meta.icon;
            var desc = "外观模式：" + meta.fullLabel + "（当前" + resolvedText + "）";
            toggleBtn.setAttribute("aria-label", desc);
            toggleBtn.setAttribute("title", desc);
        }

        var options = document.querySelectorAll("[data-theme-mode-option]");
        options.forEach(function (opt) {
            var val = opt.getAttribute("data-theme-mode-option");
            opt.setAttribute("aria-checked", val === st.mode ? "true" : "false");
        });

        var resetBtn = document.querySelector("[data-theme-reset]");
        if (resetBtn) {
            resetBtn.disabled = !st.hasOverride;
            resetBtn.setAttribute("aria-disabled", st.hasOverride ? "false" : "true");
        }

        var previewBtns = document.querySelectorAll("[data-preview-mode-option]");
        previewBtns.forEach(function (btn) {
            var val = btn.getAttribute("data-preview-mode-option");
            var active = val === st.previewChoice;
            btn.classList.toggle("is-active", active);
            btn.setAttribute("aria-pressed", active ? "true" : "false");
        });
    }

    function setThemeMenuOpen(open) {
        var btn = document.querySelector("[data-theme-toggle]");
        var menu = document.querySelector("[data-theme-menu]");
        if (!btn || !menu) return;
        menu.hidden = !open;
        btn.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function bindThemeControls(scope) {
        var root = scope || document;
        var api = getThemeApi();
        var switcher = root.querySelector ? root.querySelector("[data-theme-switcher]") : null;
        var btn = root.querySelector ? root.querySelector("[data-theme-toggle]") : null;
        var menu = root.querySelector ? root.querySelector("[data-theme-menu]") : null;

        if (btn && menu && btn.dataset.uiBound !== "1") {
            btn.dataset.uiBound = "1";
            btn.addEventListener("click", function (evt) {
                evt.stopPropagation();
                setThemeMenuOpen(menu.hidden);
            });
        }

        if (switcher && switcher.dataset.uiBound !== "1") {
            switcher.dataset.uiBound = "1";
            switcher.querySelectorAll("[data-theme-mode-option]").forEach(function (opt) {
                opt.addEventListener("click", function () {
                    var nextMode = opt.getAttribute("data-theme-mode-option");
                    var st = api.setMode(nextMode);
                    syncThemeUI(st);
                    setThemeMenuOpen(false);
                });
            });
            var resetBtn = switcher.querySelector("[data-theme-reset]");
            if (resetBtn) {
                resetBtn.addEventListener("click", function () {
                    var st = api.reset();
                    syncThemeUI(st);
                    setThemeMenuOpen(false);
                });
            }
        }

        if (root.querySelectorAll) {
            root.querySelectorAll("[data-preview-mode-option]").forEach(function (pBtn) {
                if (pBtn.dataset.uiBound === "1") return;
                pBtn.dataset.uiBound = "1";
                pBtn.addEventListener("click", function () {
                    var nextPreview = pBtn.getAttribute("data-preview-mode-option");
                    var st = api.setMode(nextPreview);
                    syncThemeUI(st);
                });
            });
        }
    }

    function initThemeController() {
        var api = getThemeApi();
        var state = api.sync({ silent: true });
        syncThemeUI(state);
        bindThemeControls(document);

        if (globalThemeListenersBound) return;
        globalThemeListenersBound = true;

        document.addEventListener("click", function (evt) {
            var switcher = document.querySelector("[data-theme-switcher]");
            var menu = document.querySelector("[data-theme-menu]");
            if (!switcher || !menu || menu.hidden) return;
            if (!switcher.contains(evt.target)) {
                setThemeMenuOpen(false);
            }
        });

        document.addEventListener("keydown", function (evt) {
            if (evt.key !== "Escape") return;
            var menu = document.querySelector("[data-theme-menu]");
            var btn = document.querySelector("[data-theme-toggle]");
            if (menu && !menu.hidden) {
                setThemeMenuOpen(false);
                if (btn) btn.focus();
            }
        });

        if (window.matchMedia) {
            var mql = window.matchMedia("(prefers-color-scheme: dark)");
            var onMediaChange = function () {
                var st = api.sync();
                syncThemeUI(st);
            };
            if (typeof mql.addEventListener === "function") {
                mql.addEventListener("change", onMediaChange);
            } else if (typeof mql.addListener === "function") {
                mql.addListener(onMediaChange);
            }
        }

        window.addEventListener("storage", function (evt) {
            if (!evt.key || evt.key === api.STORAGE_KEY) {
                var st = api.sync();
                syncThemeUI(st);
            }
        });

        window.addEventListener("pageshow", function () {
            var st = api.sync();
            syncThemeUI(st);
        });

        window.addEventListener("myblog:theme:change", function (evt) {
            syncThemeUI(evt.detail);
        });
    }

    function closeOtherDialogs(exceptDialog) {
        var openDialogs = document.querySelectorAll("dialog[open]");
        openDialogs.forEach(function (dlg) {
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

    function initSiteDirectoryDrawer() {
        var dialog = document.getElementById("site-directory-dialog");
        var slot = dialog ? dialog.querySelector("[data-dir-dialog-slot]") : null;
        var fallback = document.querySelector("[data-dir-fallback]");
        var openBtn = document.querySelector("[data-dir-open]");
        var closeBtn = dialog ? dialog.querySelector("[data-dir-close]") : null;
        var treeHost = document.querySelector("[data-dir-tree-host]");

        if (!dialog || !slot || !fallback || !openBtn || !treeHost) return;
        if (typeof dialog.showModal !== "function") return;
        if (dialog.dataset.dirBound === "1") return;
        dialog.dataset.dirBound = "1";

        // 零 FOUC：将默认闭合的降级容器内唯一树节点原位迁入 <dialog>，并切换触发按钮
        fallback.open = false;
        slot.appendChild(treeHost);
        fallback.hidden = true;
        openBtn.hidden = false;

        function closeDrawer() {
            if (dialog.open) {
                dialog.close();
            }
            openBtn.setAttribute("aria-expanded", "false");
        }

        openBtn.addEventListener("click", function () {
            if (dialog.open) {
                closeDrawer();
                return;
            }
            closeOtherDialogs(dialog);
            var navCheck = document.getElementById("nav-toggle-check");
            if (navCheck && navCheck.checked) {
                navCheck.checked = false;
            }
            setThemeMenuOpen(false);
            dialog.showModal();
            openBtn.setAttribute("aria-expanded", "true");
        });

        if (closeBtn) {
            closeBtn.addEventListener("click", closeDrawer);
        }

        dialog.addEventListener("click", function (evt) {
            if (evt.target === dialog) {
                closeDrawer();
            }
        });

        dialog.addEventListener("close", function () {
            openBtn.setAttribute("aria-expanded", "false");
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
            syncThemeUI(getThemeApi().getState());
        });
    }

    function mountAll(root) {
        var scope = root || document;
        bindThemeControls(scope);
        mountSelects(scope);
        mountDates(scope);
        mountCarousels(scope);
    }

    window.MyblogUI = {
        mount: mountAll,
        applyTheme: function (themeOrMode) {
            var st = getThemeApi().setMode(themeOrMode);
            syncThemeUI(st);
            return st;
        },
        setThemeMode: function (mode) {
            var st = getThemeApi().setMode(mode);
            syncThemeUI(st);
            return st;
        },
        resetTheme: function () {
            var st = getThemeApi().reset();
            syncThemeUI(st);
            return st;
        },
        getThemeState: function () {
            return getThemeApi().getState();
        },
    };

    document.addEventListener("DOMContentLoaded", function () {
        initThemeController();
        initSiteDirectoryDrawer();
        initHtmxCsrf();
        mountAll(document);
    });
})();
