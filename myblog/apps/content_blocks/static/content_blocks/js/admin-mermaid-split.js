/**
 * Wagtail 8.1 后台 Mermaid 左右分栏实时预览控件 (admin-mermaid-split.js)
 */

(function (window, document) {
    "use strict";

    var THEME_LABELS = {
        default: "默认浅色 (default)",
        dark: "深色模式 (dark)",
        forest: "自然绿色 (forest)",
        neutral: "简约黑白 (neutral)"
    };

    class BoundMermaidSplitWidget {
        constructor(root, name, parentCapabilities) {
            this.root = root;
            this.input = root.querySelector('textarea[name="' + name + '"]');
            if (!this.input) throw new Error('No textarea found with name "' + name + '"');
            this.canvas = root.querySelector("[data-cb-admin-mermaid-canvas]");
            this.errorEl = root.querySelector("[data-cb-admin-mermaid-error]");
            this.themeLabelEl = root.querySelector("[data-cb-admin-mermaid-theme-label]");
            this.idForLabel = this.input.id;
            this.parentCapabilities = parentCapabilities || new Map();
            this.debounceTimer = null;
            this.latestReqId = 0;

            this._bindEvents();
            this._scheduleRender();
        }

        _findThemeSelect() {
            // 1. 优先在同属 StructBlock 容器内查找，不受 StreamField 块增删/排序改名影响
            var structContainer = this.root.closest(".struct-block");
            if (!structContainer) {
                var cp = this.root.closest("[data-contentpath]");
                structContainer = cp ? cp.parentElement : null;
            }
            if (structContainer) {
                var siblingSel = structContainer.querySelector('[data-contentpath="theme"] select, select[name$="-theme"]');
                if (siblingSel) return siblingSel;
            }
            // 2. 兜底按当前字段精确前缀在所属 form / document 中查找
            var inputName = this.input.name || "";
            if (inputName.endsWith("-diagram_code")) {
                var expectedThemeName = inputName.slice(0, -"-diagram_code".length) + "-theme";
                var formScope = this.input.closest("form") || document;
                var exact = formScope.querySelector('select[name="' + expectedThemeName + '"]');
                if (exact) return exact;
            }
            return null;
        }

        _getTheme() {
            var sel = this._findThemeSelect();
            var raw = (sel && sel.value) || "default";
            return window.CBMermaidRenderer ? window.CBMermaidRenderer.normalizeTheme(raw) : raw;
        }

        _updateThemeVisuals(theme) {
            if (this.canvas) {
                this.canvas.setAttribute("data-theme", theme);
            }
            if (this.themeLabelEl) {
                this.themeLabelEl.textContent = "主题: " + (THEME_LABELS[theme] || theme);
                this.themeLabelEl.setAttribute("data-theme", theme);
            }
        }

        _bindEvents() {
            var self = this;
            this.input.addEventListener("input", function () {
                self.latestReqId++;
                self._scheduleRender();
            });
            // 注意：Telepath 挂载时 widget 可能尚未插入 <form>，且后面的 theme 字段可能后渲染；
            // 在 document 上委托并在控件移出 DOM 后自动解绑，确保新增/复制/移动块均可实时切换主题。
            this._onDocThemeChange = function (e) {
                if (!self.root.isConnected) {
                    document.removeEventListener("change", self._onDocThemeChange);
                    document.removeEventListener("input", self._onDocThemeChange);
                    return;
                }
                var target = e.target;
                if (!target || target.tagName !== "SELECT") return;
                var themeSel = self._findThemeSelect();
                if (target === themeSel) {
                    if (self.debounceTimer) {
                        clearTimeout(self.debounceTimer);
                        self.debounceTimer = null;
                    }
                    self.latestReqId++;
                    self._renderPreview();
                }
            };
            document.addEventListener("change", this._onDocThemeChange);
            document.addEventListener("input", this._onDocThemeChange);
        }

        _scheduleRender() {
            var self = this;
            if (this.debounceTimer) clearTimeout(this.debounceTimer);
            this.debounceTimer = setTimeout(function () {
                self._renderPreview();
            }, 250);
        }

        _renderPreview() {
            var self = this;
            var code = (this.input.value || "").trim();
            var theme = this._getTheme();
            this._updateThemeVisuals(theme);
            var reqId = ++this.latestReqId;
            if (!code) {
                this.errorEl.hidden = true;
                this.canvas.innerHTML = '<span class="cb-admin-placeholder">在左侧输入 Mermaid 语法即可实时预览...</span>';
                return;
            }
            if (!window.CBMermaidRenderer) {
                this.errorEl.hidden = false;
                this.errorEl.textContent = "Mermaid 公共渲染引擎未加载";
                return;
            }

            window.CBMermaidRenderer.render(code, theme).then(function (res) {
                if (reqId !== self.latestReqId || !self.root.isConnected) return;
                self.errorEl.hidden = true;
                self._updateThemeVisuals(res.theme);
                self.canvas.innerHTML = "";
                self.canvas.appendChild(res.svgNode);
            }).catch(function (err) {
                if (reqId !== self.latestReqId || !self.root.isConnected) return;
                self.errorEl.hidden = false;
                self.errorEl.textContent = "语法解析提示（保留上次正确预览）: " + (err.message || "请检查语法");
            });
        }

        getValue() { return this.input.value; }
        getState() { return this.input.value; }
        setState(val) {
            this.input.value = val == null ? "" : String(val);
            this._scheduleRender();
        }
        setInvalid(isInvalid) {
            if (isInvalid) this.input.setAttribute("aria-invalid", "true");
            else this.input.removeAttribute("aria-invalid");
        }
        getValueForLabel() { return this.getValue(); }
        getTextLabel(opts) {
            var text = String(this.getValueForLabel() || "").trim();
            if (!text) return null;
            var maxLen = opts && opts.maxLength;
            return maxLen && text.length > maxLen ? text.substring(0, maxLen - 1) + "…" : text;
        }
        focus() { this.input.focus(); }
        setCapabilityOptions(capability, options) {
            var existing = this.parentCapabilities.get(capability);
            if (existing) Object.assign(existing, options);
        }
    }

    class MermaidSplitWidgetAdapter {
        constructor(htmlTemplate) { this.html = htmlTemplate; }
        render(placeholder, name, id, initialState, parentCapabilities, options) {
            var renderedHtml = this.html.replace(/__NAME__/g, name).replace(/__ID__/g, id);
            var container = document.createElement("div");
            container.innerHTML = renderedHtml.trim();
            var root = container.firstElementChild;
            placeholder.replaceWith(root);
            if (options && options.attributes) {
                Object.keys(options.attributes).forEach(function (k) { root.setAttribute(k, options.attributes[k]); });
            }
            var widget = new BoundMermaidSplitWidget(root, name, parentCapabilities);
            widget.setState(initialState);
            return widget;
        }
        getByName(name, container) {
            var root = container.querySelector("[data-cb-admin-mermaid-field]") || container;
            return new BoundMermaidSplitWidget(root, name);
        }
    }

    if (window.telepath && typeof window.telepath.register === "function") {
        window.telepath.register("content_blocks.widgets.MermaidSplitWidget", MermaidSplitWidgetAdapter);
    }
})(window, document);
