/**
 * 全站公共 UI 组件统一挂载桥接器 (MyblogUI)
 *
 * 涵盖：
 * 1. 三态昼夜主题控制器（复用 base.html 注入的 window.__myblogTheme 单源状态机）；
 * 2. 全站栏目目录抽屉与实时过滤；
 * 3. 全站 Ctrl+K / `/` 快捷搜索命令面板（接入 /search/quick/）；
 * 4. Tom Select ([data-ui-select])：可搜索下拉框与多选标签胶囊；
 * 5. Flatpickr ([data-ui-date])：统一桌面/iPad/手机的中文日期选择框；
 * 6. Splide ([data-ui-carousel])：自适应响应式触控轮播图；
 * 7. HTMX CSRF 自动注入与局部刷新自动重新挂载。
 */
(function () {
    "use strict";

    var globalThemeListenersBound = false;
    var MODE_META = {
        system: { shortLabel: "系统", fullLabel: "跟随系统" },
        light: { shortLabel: "白天", fullLabel: "白天（浅色）" },
        dark: { shortLabel: "黑夜", fullLabel: "黑夜（深色）" },
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
            if (iconEl) {
                var glyphs = iconEl.querySelectorAll("[data-theme-icon-mode]");
                glyphs.forEach(function (g) {
                    g.hidden = g.getAttribute("data-theme-icon-mode") !== st.mode;
                });
            }
            if (labelEl) labelEl.textContent = meta.shortLabel;
            if (!iconEl && !labelEl) toggleBtn.textContent = meta.shortLabel;
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

        var filterInput = dialog.querySelector("[data-dir-filter]");
        var expandAllBtn = dialog.querySelector("[data-dir-expand-all]");
        var collapseAllBtn = dialog.querySelector("[data-dir-collapse-all]");
        var emptyHint = dialog.querySelector("[data-dir-empty]");
        var hasFilterSnapshot = false;

        function snapshotDefaultOpenState() {
            treeHost.querySelectorAll("details.site-dir-branch-details").forEach(function (det) {
                det.dataset.dirDefaultOpen = det.open ? "1" : "0";
            });
            hasFilterSnapshot = true;
        }

        function restoreDefaultTreeState() {
            treeHost.querySelectorAll(".site-dir-item").forEach(function (item) {
                item.hidden = false;
            });
            if (hasFilterSnapshot) {
                treeHost.querySelectorAll("details.site-dir-branch-details").forEach(function (det) {
                    det.open = det.dataset.dirDefaultOpen === "1";
                });
                hasFilterSnapshot = false;
            }
            if (emptyHint) {
                emptyHint.hidden = true;
            }
        }

        function filterList(listEl, needle) {
            if (!listEl) return false;
            var anyVisible = false;
            Array.prototype.forEach.call(listEl.children, function (item) {
                if (!item.classList || !item.classList.contains("site-dir-item")) return;
                var rawTitle = item.getAttribute("data-dir-title");
                if (rawTitle === null) {
                    var link = item.querySelector(".site-dir-link");
                    rawTitle = link ? link.textContent : "";
                }
                var titleText = (rawTitle || "").toLowerCase();
                var selfMatched = titleText.indexOf(needle) !== -1;

                var branchDetails = null;
                var childMatched = false;
                var wrap = item.firstElementChild;
                if (wrap) {
                    branchDetails = wrap.querySelector(":scope > details.site-dir-branch-details");
                    if (branchDetails) {
                        var subList = branchDetails.querySelector(":scope > ul.site-dir-list");
                        childMatched = filterList(subList, needle);
                    }
                }

                var visible = selfMatched || childMatched;
                item.hidden = !visible;
                if (branchDetails) {
                    branchDetails.open = childMatched;
                }
                if (visible) {
                    anyVisible = true;
                }
            });
            return anyVisible;
        }

        function applyDirectoryFilter() {
            var needle = filterInput ? filterInput.value.trim().toLowerCase() : "";
            if (!needle) {
                restoreDefaultTreeState();
                return;
            }
            if (!hasFilterSnapshot) {
                snapshotDefaultOpenState();
            }
            var rootList = treeHost.querySelector("ul.site-dir-list");
            var hasMatch = filterList(rootList, needle);
            if (emptyHint) {
                emptyHint.hidden = hasMatch;
            }
        }

        if (filterInput) {
            filterInput.addEventListener("input", applyDirectoryFilter);
        }

        if (expandAllBtn) {
            expandAllBtn.addEventListener("click", function () {
                treeHost.querySelectorAll("details.site-dir-branch-details").forEach(function (det) {
                    det.open = true;
                    if (!filterInput || !filterInput.value.trim()) {
                        det.dataset.dirDefaultOpen = "1";
                    }
                });
            });
        }

        if (collapseAllBtn) {
            collapseAllBtn.addEventListener("click", function () {
                treeHost.querySelectorAll("details.site-dir-branch-details").forEach(function (det) {
                    det.open = false;
                    if (!filterInput || !filterInput.value.trim()) {
                        det.dataset.dirDefaultOpen = "0";
                    }
                });
            });
        }
    }

    function initQuickSearchPalette() {
        var dialog = document.getElementById("quick-search-dialog");
        var input = dialog ? dialog.querySelector("[data-quick-search-input]") : null;
        var resultsEl = dialog ? dialog.querySelector("[data-quick-search-results]") : null;
        var closeBtn = dialog ? dialog.querySelector("[data-quick-search-close]") : null;
        var fullLink = dialog ? dialog.querySelector("[data-quick-search-full-link]") : null;
        var openTriggers = document.querySelectorAll("[data-quick-search-open]");

        if (!dialog || !input || !resultsEl) return;
        if (typeof dialog.showModal !== "function") return;
        if (dialog.dataset.quickSearchBound === "1") return;
        dialog.dataset.quickSearchBound = "1";

        var quickUrl = dialog.getAttribute("data-quick-search-url") || "/search/quick/";
        var searchPageUrl = dialog.getAttribute("data-search-page-url") || "/search/";
        var debounceTimer = null;
        var abortController = null;
        var selectedIndex = -1;
        var resultItems = [];

        function isEditableTarget(target) {
            if (!target) return false;
            if (target.isContentEditable) return true;
            var tag = (target.tagName || "").toUpperCase();
            if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
            return Boolean(target.closest && target.closest("input, textarea, select, [contenteditable]"));
        }

        function safeHref(rawUrl) {
            if (typeof rawUrl !== "string") return "#";
            var trimmed = rawUrl.trim();
            if (trimmed.charAt(0) === "/" || /^https?:\/\//i.test(trimmed)) {
                return trimmed;
            }
            return "#";
        }

        function buildFullSearchUrl(q) {
            var trimmed = (q || "").trim();
            if (!trimmed) return searchPageUrl;
            var sep = searchPageUrl.indexOf("?") === -1 ? "?" : "&";
            return searchPageUrl + sep + "query=" + encodeURIComponent(trimmed);
        }

        function updateFullSearchLink(q) {
            if (!fullLink) return;
            var trimmed = (q || "").trim();
            fullLink.setAttribute("href", buildFullSearchUrl(trimmed));
            fullLink.textContent = trimmed ? ("查看 “" + trimmed + "” 完整搜索结果 →") : "前往完整搜索页 →";
        }

        function clearResultsContainer() {
            while (resultsEl.firstChild) {
                resultsEl.removeChild(resultsEl.firstChild);
            }
            resultItems = [];
            selectedIndex = -1;
            input.removeAttribute("aria-activedescendant");
        }

        function setSelectedIndex(nextIdx) {
            if (!resultItems.length) {
                selectedIndex = -1;
                input.removeAttribute("aria-activedescendant");
                return;
            }
            selectedIndex = ((nextIdx % resultItems.length) + resultItems.length) % resultItems.length;
            resultItems.forEach(function (el, idx) {
                var active = idx === selectedIndex;
                el.classList.toggle("is-selected", active);
                el.setAttribute("aria-selected", active ? "true" : "false");
            });
            var activeItem = resultItems[selectedIndex];
            input.setAttribute("aria-activedescendant", activeItem.id);
            if (typeof activeItem.scrollIntoView === "function") {
                activeItem.scrollIntoView({ block: "nearest" });
            }
        }

        function renderMessage(text, isError) {
            clearResultsContainer();
            var p = document.createElement("p");
            p.className = isError ? "quick-search-status is-error" : "quick-search-status";
            p.textContent = text;
            resultsEl.appendChild(p);
        }

        function appendGroup(label, items, buildRowFn) {
            if (!Array.isArray(items) || !items.length) return;
            var groupEl = document.createElement("div");
            groupEl.className = "quick-search-group";

            var titleEl = document.createElement("div");
            titleEl.className = "quick-search-group-title";
            titleEl.textContent = label;
            groupEl.appendChild(titleEl);

            var listEl = document.createElement("div");
            listEl.className = "quick-search-group-list";
            items.forEach(function (item) {
                var row = buildRowFn(item, resultItems.length);
                if (row) {
                    resultItems.push(row);
                    listEl.appendChild(row);
                }
            });
            if (listEl.childNodes.length) {
                groupEl.appendChild(listEl);
                resultsEl.appendChild(groupEl);
            }
        }

        function createOptionLink(href, idx) {
            var a = document.createElement("a");
            a.className = "quick-search-item";
            a.id = "quick-search-opt-" + idx;
            a.setAttribute("role", "option");
            a.setAttribute("aria-selected", "false");
            a.setAttribute("href", safeHref(href));
            a.addEventListener("click", function () {
                closeQuickSearch();
            });
            return a;
        }

        function renderResults(payload, requestedQuery) {
            clearResultsContainer();
            var dirs = payload && Array.isArray(payload.directories) ? payload.directories : [];
            var arts = payload && Array.isArray(payload.articles) ? payload.articles : [];
            var tags = payload && Array.isArray(payload.tags) ? payload.tags : [];

            appendGroup("栏目目录", dirs, function (d, idx) {
                var a = createOptionLink(d.url, idx);
                var titleSpan = document.createElement("span");
                titleSpan.className = "quick-search-item-title";
                titleSpan.textContent = d.title || "";
                var metaSpan = document.createElement("span");
                metaSpan.className = "quick-search-item-meta";
                metaSpan.textContent = String(d.article_count || 0) + " 篇";
                a.appendChild(titleSpan);
                a.appendChild(metaSpan);
                return a;
            });

            appendGroup("文章", arts, function (art, idx) {
                var a = createOptionLink(art.url, idx);
                var titleSpan = document.createElement("span");
                titleSpan.className = "quick-search-item-title";
                titleSpan.textContent = art.title || "";
                var metaSpan = document.createElement("span");
                metaSpan.className = "quick-search-item-meta";
                if (art.authored_on) {
                    var dateSpan = document.createElement("span");
                    dateSpan.className = "quick-search-item-date";
                    dateSpan.textContent = art.authored_on;
                    metaSpan.appendChild(dateSpan);
                }
                if (art.type_label) {
                    var badgeSpan = document.createElement("span");
                    badgeSpan.className = "quick-search-item-badge";
                    badgeSpan.textContent = art.type_label;
                    metaSpan.appendChild(badgeSpan);
                }
                a.appendChild(titleSpan);
                a.appendChild(metaSpan);
                return a;
            });

            appendGroup("标签", tags, function (tg, idx) {
                var a = createOptionLink(tg.url, idx);
                var titleSpan = document.createElement("span");
                titleSpan.className = "quick-search-item-title";
                titleSpan.textContent = "#" + (tg.name || tg.slug || "");
                var metaSpan = document.createElement("span");
                metaSpan.className = "quick-search-item-meta";
                metaSpan.textContent = String(tg.count || 0) + " 篇";
                a.appendChild(titleSpan);
                a.appendChild(metaSpan);
                return a;
            });

            if (!resultItems.length) {
                renderMessage(
                    requestedQuery
                        ? "未找到匹配的栏目、文章或标签，按 Enter 前往完整搜索页"
                        : "暂无可展示的公开栏目",
                    false
                );
            }
        }

        function fetchQuickSearch(query) {
            if (abortController) {
                abortController.abort();
                abortController = null;
            }
            if (typeof window.AbortController === "function") {
                abortController = new window.AbortController();
            }
            var sep = quickUrl.indexOf("?") === -1 ? "?" : "&";
            var reqUrl = quickUrl + sep + "q=" + encodeURIComponent(query || "");
            fetch(reqUrl, {
                method: "GET",
                headers: {
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
                signal: abortController ? abortController.signal : undefined,
            })
                .then(function (resp) {
                    if (!resp.ok) throw new Error("HTTP " + resp.status);
                    return resp.json();
                })
                .then(function (data) {
                    if (!dialog.open) return;
                    renderResults(data, query);
                })
                .catch(function (err) {
                    if (err && err.name === "AbortError") return;
                    if (!dialog.open) return;
                    renderMessage("快捷搜索暂时不可用，请按 Enter 前往完整搜索页", true);
                });
        }

        function closeQuickSearch() {
            if (debounceTimer) {
                clearTimeout(debounceTimer);
                debounceTimer = null;
            }
            if (abortController) {
                abortController.abort();
                abortController = null;
            }
            if (dialog.open) {
                dialog.close();
            }
        }

        function openQuickSearch() {
            closeOtherDialogs(dialog);
            var navCheck = document.getElementById("nav-toggle-check");
            if (navCheck && navCheck.checked) {
                navCheck.checked = false;
            }
            setThemeMenuOpen(false);
            dialog.showModal();
            input.focus();
            if (input.value && typeof input.select === "function") {
                input.select();
            }
            var trimmed = input.value.trim();
            updateFullSearchLink(trimmed);
            fetchQuickSearch(trimmed);
        }

        openTriggers.forEach(function (trigger) {
            trigger.addEventListener("click", function (evt) {
                evt.preventDefault();
                if (dialog.open) {
                    closeQuickSearch();
                } else {
                    openQuickSearch();
                }
            });
        });

        if (closeBtn) {
            closeBtn.addEventListener("click", closeQuickSearch);
        }

        dialog.addEventListener("click", function (evt) {
            if (evt.target === dialog) {
                closeQuickSearch();
            }
        });

        dialog.addEventListener("close", function () {
            if (debounceTimer) {
                clearTimeout(debounceTimer);
                debounceTimer = null;
            }
            if (abortController) {
                abortController.abort();
                abortController = null;
            }
        });

        input.addEventListener("input", function () {
            var q = input.value.trim();
            updateFullSearchLink(q);
            if (debounceTimer) clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function () {
                fetchQuickSearch(q);
            }, 160);
        });

        dialog.addEventListener("keydown", function (evt) {
            if (evt.key === "ArrowDown") {
                if (!resultItems.length) return;
                evt.preventDefault();
                setSelectedIndex(selectedIndex === -1 ? 0 : selectedIndex + 1);
            } else if (evt.key === "ArrowUp") {
                if (!resultItems.length) return;
                evt.preventDefault();
                setSelectedIndex(selectedIndex === -1 ? resultItems.length - 1 : selectedIndex - 1);
            } else if (evt.key === "Enter") {
                if (selectedIndex >= 0 && resultItems[selectedIndex]) {
                    evt.preventDefault();
                    var targetHref = resultItems[selectedIndex].getAttribute("href");
                    closeQuickSearch();
                    if (targetHref && targetHref !== "#") {
                        window.location.href = targetHref;
                    }
                } else {
                    var q = input.value.trim();
                    if (q) {
                        evt.preventDefault();
                        closeQuickSearch();
                        window.location.href = buildFullSearchUrl(q);
                    }
                }
            }
        });

        document.addEventListener("keydown", function (evt) {
            if ((evt.ctrlKey || evt.metaKey) && !evt.altKey && (evt.key === "k" || evt.key === "K")) {
                evt.preventDefault();
                if (dialog.open) {
                    closeQuickSearch();
                } else {
                    openQuickSearch();
                }
                return;
            }
            if (!evt.ctrlKey && !evt.metaKey && !evt.altKey && evt.key === "/") {
                if (!dialog.open && !isEditableTarget(evt.target)) {
                    evt.preventDefault();
                    openQuickSearch();
                }
            }
        });
    }

    function initHeaderScrollState() {
        var header = document.querySelector(".site-header");
        if (!header || header.dataset.scrollBound === "1") return;
        header.dataset.scrollBound = "1";
        var syncScroll = function () {
            var y = window.scrollY !== undefined ? window.scrollY : (window.pageYOffset || 0);
            header.classList.toggle("is-scrolled", y > 8);
        };
        syncScroll();
        window.addEventListener("scroll", syncScroll, { passive: true });
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
        mountCollapsibleIntros(scope);
    }

    function mountCollapsibleIntros(root) {
        var nodes = (root || document).querySelectorAll("[data-intro-collapsible]");
        nodes.forEach(function (box) {
            if (box.dataset.introBound === "1") return;
            box.dataset.introBound = "1";

            var wrap = box.parentElement;
            var btn = wrap ? wrap.querySelector("[data-intro-toggle]") : null;
            if (!btn) return;

            var labelEl = btn.querySelector("span") || btn;
            box.classList.add("is-collapsed");
            box.classList.remove("is-expanded");
            btn.setAttribute("aria-expanded", "false");
            labelEl.textContent = "展开完整导读";

            btn.addEventListener("click", function () {
                var expanded = btn.getAttribute("aria-expanded") === "true";
                if (expanded) {
                    box.classList.add("is-collapsed");
                    box.classList.remove("is-expanded");
                    btn.setAttribute("aria-expanded", "false");
                    labelEl.textContent = "展开完整导读";
                } else {
                    box.classList.remove("is-collapsed");
                    box.classList.add("is-expanded");
                    btn.setAttribute("aria-expanded", "true");
                    labelEl.textContent = "收起导读";
                }
            });
        });
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
        initQuickSearchPalette();
        initHeaderScrollState();
        initHtmxCsrf();
        mountAll(document);
    });
})();
