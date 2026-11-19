/**
 * Wagtail 8.1 后台复杂表格左右分栏实时预览控件 (admin-table-split.js)
 */

(function (window, document) {
    "use strict";

    var SAMPLE_TABLE_JSON = {
        rows: [
            [
                { value: "模块", rowspan: 2, header: true },
                { value: "核心能力演示", colspan: 3, header: true }
            ],
            [
                { value: "数学公式 (KaTeX)", header: true },
                { value: "代码高亮", header: true },
                { value: "插图与尺寸", header: true }
            ],
            [
                { value: "事务引擎", rowspan: 2 },
                "$T(n) = O(\\log n)$",
                "```sql\nSELECT id, status\nFROM pages\nWHERE is_recycled = FALSE;\n```",
                "替换为你的图片：<br>`![示意图|200](asset://image/UUID)`"
            ],
            [
                "$$\\sum_{i=1}^{n} w_i x_i$$",
                "`git status --short`",
                { value: "支持 `|240`、`|320x180`、`|50%,center`", align: "center" }
            ]
        ]
    };

    function getCsrfToken(root) {
        var form = root ? root.closest("form") : null;
        var inp = (form && form.querySelector('input[name="csrfmiddlewaretoken"]')) ||
            document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (inp && inp.value) return inp.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    class BoundTableJsonSplitWidget {
        constructor(root, name, parentCapabilities) {
            this.root = root;
            this.input = root.querySelector('textarea[name="' + name + '"]');
            if (!this.input) throw new Error('No textarea found with name "' + name + '"');
            this.canvas = root.querySelector("[data-cb-admin-table-canvas]");
            this.errorEl = root.querySelector("[data-cb-admin-table-error]");
            this.formatBtn = root.querySelector("[data-cb-table-format-btn]");
            this.sampleBtn = root.querySelector("[data-cb-table-sample-btn]");
            this.idForLabel = this.input.id;
            this.parentCapabilities = parentCapabilities || new Map();
            this.debounceTimer = null;
            this.latestReqId = 0;

            this._bindEvents();
            this._schedulePreview();
        }

        _structParent() {
            var cp = this.root.closest("[data-contentpath]");
            return cp ? cp.parentElement : null;
        }

        _collectStructOptions() {
            var p = this._structParent();
            if (!p) return { caption: "", has_header_row: true, has_header_col: false, alignment: "center" };
            var cap = p.querySelector('input[name$="-caption"]');
            var hdrRow = p.querySelector('input[name$="-has_header_row"]');
            var hdrCol = p.querySelector('input[name$="-has_header_col"]');
            var align = p.querySelector('select[name$="-alignment"]');
            return {
                caption: cap ? cap.value : "",
                has_header_row: hdrRow ? hdrRow.checked : true,
                has_header_col: hdrCol ? hdrCol.checked : false,
                alignment: align ? align.value : "center"
            };
        }

        _bindEvents() {
            var self = this;
            this.input.addEventListener("input", function () {
                self._schedulePreview();
            });

            if (this.formatBtn) {
                this.formatBtn.addEventListener("click", function () {
                    try {
                        var parsed = JSON.parse(self.input.value || "{}");
                        self.input.value = JSON.stringify(parsed, null, 2);
                        self.input.dispatchEvent(new Event("input", { bubbles: true }));
                        self.input.dispatchEvent(new Event("change", { bubbles: true }));
                        self.errorEl.hidden = true;
                    } catch (err) {
                        self.errorEl.hidden = false;
                        self.errorEl.textContent = "JSON 格式化失败: " + err.message;
                    }
                });
            }

            if (this.sampleBtn) {
                this.sampleBtn.addEventListener("click", function () {
                    var cur = (self.input.value || "").trim();
                    var isEmpty = !cur || cur === "{}" || cur === '{"rows": []}' || cur === '{"rows":[]}';
                    if (!isEmpty) {
                        try {
                            var p = JSON.parse(cur);
                            if (p && Array.isArray(p.rows) && p.rows.length === 0) isEmpty = true;
                        } catch (_e) {}
                    }
                    if (!isEmpty && !window.confirm("当前表格已有数据，确定替换为复杂表格示例吗？")) {
                        return;
                    }
                    self.input.value = JSON.stringify(SAMPLE_TABLE_JSON, null, 2);
                    self.input.dispatchEvent(new Event("input", { bubbles: true }));
                    self.input.dispatchEvent(new Event("change", { bubbles: true }));
                });
            }

            var p = this._structParent();
            if (p && !p.hasAttribute("data-cb-table-opts-bound")) {
                p.setAttribute("data-cb-table-opts-bound", "true");
                p.addEventListener("change", function (e) {
                    if (e.target !== self.input) self._schedulePreview();
                });
            }
        }

        _schedulePreview() {
            var self = this;
            if (this.debounceTimer) clearTimeout(this.debounceTimer);
            this.debounceTimer = setTimeout(function () {
                self._renderPreview();
            }, 350);
        }

        _renderPreview() {
            var self = this;
            var url = this.input.dataset.previewUrl;
            var raw = (this.input.value || "").trim();
            if (!url || !raw) return;

            var opts = this._collectStructOptions();
            opts.table_data = raw;
            var reqId = ++this.latestReqId;

            fetch(url, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCsrfToken(this.root)
                },
                body: JSON.stringify(opts)
            })
                .then(function (resp) {
                    return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
                })
                .then(function (res) {
                    if (reqId !== self.latestReqId) return;
                    if (!res.ok || !res.data || !res.data.ok) {
                        var errs = (res.data && res.data.errors) || ["表格解析失败"];
                        self.errorEl.hidden = false;
                        self.errorEl.textContent = errs.join("；");
                        return;
                    }
                    self.errorEl.hidden = true;
                    self.canvas.innerHTML = res.data.html || '<span class="cb-admin-placeholder">空表格</span>';
                    if (typeof window.renderContentMath === "function") {
                        window.renderContentMath(self.canvas);
                    }
                    if (typeof window.enhanceCodeBlocks === "function") {
                        window.enhanceCodeBlocks(self.canvas);
                    }
                })
                .catch(function (err) {
                    if (reqId !== self.latestReqId) return;
                    self.errorEl.hidden = false;
                    self.errorEl.textContent = "预览请求异常: " + (err.message || "网络错误");
                });
        }

        getValue() { return this.input.value; }
        getState() { return this.input.value; }
        setState(val) {
            this.input.value = val == null ? "" : String(val);
            this._schedulePreview();
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

    class TableJsonSplitWidgetAdapter {
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
            var widget = new BoundTableJsonSplitWidget(root, name, parentCapabilities);
            widget.setState(initialState);
            return widget;
        }
        getByName(name, container) {
            var root = container.querySelector("[data-cb-admin-table-field]") || container;
            return new BoundTableJsonSplitWidget(root, name);
        }
    }

    if (window.telepath && typeof window.telepath.register === "function") {
        window.telepath.register("content_blocks.widgets.TableJsonSplitWidget", TableJsonSplitWidgetAdapter);
    }
})(window, document);
