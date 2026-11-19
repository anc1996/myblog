/**
 * Wagtail 8.1 后台 Vditor Markdown 分屏编辑器 + 剪贴板图片上传适配器 (admin-vditor-markdown.js)
 */

(function (window, document) {
    "use strict";

    var UPLOAD_TOKEN_RE = /<!--cb-vditor-upload:[A-Za-z0-9]+-->/g;
    var pendingUploadsByForm = new WeakMap();

    function getCsrfToken(root) {
        var form = root ? root.closest("form") : null;
        var inp = (form && form.querySelector('input[name="csrfmiddlewaretoken"]')) ||
            document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (inp && inp.value) return inp.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function stripTransientUploadTokens(value) {
        return String(value || "").replace(UPLOAD_TOKEN_RE, "");
    }

    function dispatchTextareaEvents(textarea) {
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
        textarea.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function bindFormSubmitGuard(form) {
        if (!form || form.hasAttribute("data-cb-vditor-guard-bound")) return;
        form.setAttribute("data-cb-vditor-guard-bound", "true");
        form.addEventListener("submit", function (e) {
            var count = pendingUploadsByForm.get(form) || 0;
            if (count > 0) {
                e.preventDefault();
                window.alert("仍有图片正在上传中，请等待上传完成后再保存或发布。");
            }
        });
    }

    function adjustPending(form, delta) {
        if (!form) return;
        var cur = pendingUploadsByForm.get(form) || 0;
        pendingUploadsByForm.set(form, Math.max(0, cur + delta));
    }

    function rewritePreviewAssets(htmlStr) {
        if (!htmlStr || htmlStr.indexOf("asset://") === -1) return htmlStr;
        return htmlStr
            .replace(/asset:\/\/image\/([0-9a-fA-F-]{36})/g, "/media-gateway/image/$1/")
            .replace(/asset:\/\/document\/([0-9a-fA-F-]{36})/g, "/media-gateway/document/$1/");
    }

    function parseImageAltAndAttrs(rawAlt) {
        var altText = String(rawAlt || "");
        var pipeIdx = altText.lastIndexOf("|");
        if (pipeIdx === -1) return { alt: altText, attrs: null };

        var baseAlt = altText.slice(0, pipeIdx);
        var spec = altText.slice(pipeIdx + 1).trim();
        if (!spec) return { alt: altText, attrs: null };

        var tokens = spec.split(/[,;\s]+/).filter(Boolean);
        if (tokens.length === 0) return { alt: altText, attrs: null };

        var presetPx = { sm: "240", md: "480", lg: "720" };
        var attrs = {};
        var classes = ["cb-img-sized"];

        for (var i = 0; i < tokens.length; i++) {
            var low = tokens[i].toLowerCase();
            if (presetPx[low]) {
                attrs.width = presetPx[low];
                continue;
            }
            if (low === "full") {
                attrs.style = "width:100%;max-width:100%";
                continue;
            }
            if (low === "left" || low === "center" || low === "right") {
                classes.push("cb-img-align-" + low);
                continue;
            }
            var mDim = low.match(/^(\d{2,4})x(\d{2,4})(?:px)?$/);
            if (mDim) {
                var w1 = parseInt(mDim[1], 10);
                var h1 = parseInt(mDim[2], 10);
                if (w1 < 16 || w1 > 4000 || h1 < 16 || h1 > 4000) return { alt: altText, attrs: null };
                attrs.width = String(w1);
                attrs.height = String(h1);
                continue;
            }
            var mPx = low.match(/^(?:(?:w|width)=)?(\d{2,4})(?:px)?$/);
            if (mPx) {
                var w2 = parseInt(mPx[1], 10);
                if (w2 < 16 || w2 > 4000) return { alt: altText, attrs: null };
                attrs.width = String(w2);
                continue;
            }
            var mH = low.match(/^(?:h|height)=(\d{2,4})(?:px)?$/);
            if (mH) {
                var h2 = parseInt(mH[1], 10);
                if (h2 < 16 || h2 > 4000) return { alt: altText, attrs: null };
                attrs.height = String(h2);
                continue;
            }
            var mPct = low.match(/^(?:(?:w|width)=)?(\d{1,3})%$/);
            if (mPct) {
                var pct = parseInt(mPct[1], 10);
                if (pct < 5 || pct > 100) return { alt: altText, attrs: null };
                attrs.style = "width:" + pct + "%;max-width:100%";
                continue;
            }
            var mAlign = low.match(/^align=(left|center|right)$/);
            if (mAlign) {
                classes.push("cb-img-align-" + mAlign[1]);
                continue;
            }
            return { alt: altText, attrs: null };
        }
        attrs.className = Array.from(new Set(classes)).join(" ");
        return { alt: baseAlt.trim(), attrs: attrs };
    }

    function applyImageSizingToElement(img) {
        if (!img || img.tagName !== "IMG") return;
        var rawAlt = img.getAttribute("alt") || "";
        var parsed = parseImageAltAndAttrs(rawAlt);
        if (parsed.attrs) {
            img.setAttribute("alt", parsed.alt);
            if (parsed.attrs.width && !img.getAttribute("width")) {
                img.setAttribute("width", parsed.attrs.width);
            }
            if (parsed.attrs.height && !img.getAttribute("height")) {
                img.setAttribute("height", parsed.attrs.height);
            }
            if (parsed.attrs.style && !img.getAttribute("style")) {
                img.setAttribute("style", parsed.attrs.style);
            }
            parsed.attrs.className.split(/\s+/).forEach(function (cls) {
                if (cls) img.classList.add(cls);
            });
        }
        img.setAttribute("data-cb-raw-alt", rawAlt);
    }

    function transformTableRichContent(table, doc) {
        var cells = table.querySelectorAll("td, th");
        cells.forEach(function (cell) {
            var walker = doc.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
            var textNodes = [];
            var node;
            while ((node = walker.nextNode())) {
                if (!node.nodeValue) continue;
                var p = node.parentElement;
                if (p && p.closest("code, pre, script, style, textarea, .language-math, .katex")) continue;
                if (
                    node.nodeValue.indexOf("`") !== -1 ||
                    node.nodeValue.indexOf("![") !== -1
                ) {
                    textNodes.push(node);
                }
            }
            textNodes.forEach(function (textNode) {
                var text = textNode.nodeValue;
                var tokenRe = /```([a-zA-Z0-9_+#.-]*)[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```|`([^`\n]+)`|!\[([^\]]*)\]\(((?:https?:\/\/|\/media-gateway\/image\/)[^\s)]+)\)/g;
                var frag = doc.createDocumentFragment();
                var lastIdx = 0;
                var m;
                var changed = false;
                while ((m = tokenRe.exec(text)) !== null) {
                    changed = true;
                    if (m.index > lastIdx) {
                        frag.appendChild(doc.createTextNode(text.slice(lastIdx, m.index)));
                    }
                    if (m[2] !== undefined) {
                        var lang = (m[1] || "plaintext").trim().toLowerCase();
                        var pre = doc.createElement("pre");
                        var code = doc.createElement("code");
                        code.className = "language-" + lang;
                        code.textContent = m[2];
                        pre.appendChild(code);
                        frag.appendChild(pre);
                    } else if (m[3] !== undefined) {
                        var inlineCode = doc.createElement("code");
                        inlineCode.className = "cb-inline-code";
                        inlineCode.textContent = m[3];
                        frag.appendChild(inlineCode);
                    } else if (m[5] !== undefined) {
                        var img = doc.createElement("img");
                        img.setAttribute("src", m[5]);
                        img.setAttribute("alt", m[4] || "");
                        img.setAttribute("loading", "lazy");
                        frag.appendChild(img);
                    }
                    lastIdx = tokenRe.lastIndex;
                }
                if (!changed) return;
                if (lastIdx < text.length) {
                    frag.appendChild(doc.createTextNode(text.slice(lastIdx)));
                }
                textNode.replaceWith(frag);
            });
        });
    }

    function isDarkAdminTheme() {
        var root = document.documentElement;
        var body = document.body;
        return (
            root.classList.contains("w-theme-dark") ||
            root.dataset.theme === "dark" ||
            (body && body.dataset.theme === "dark") ||
            (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches &&
             !root.classList.contains("w-theme-light"))
        );
    }

    function getAdminFullscreenBounds() {
        var main = document.getElementById("main");
        var header = main && main.querySelector(".w-slim-header");
        var top = header ? Math.max(0, Math.round(header.getBoundingClientRect().bottom)) : 0;
        var left = main ? Math.max(0, Math.round(main.getBoundingClientRect().left)) : 0;
        var width = Math.max(320, window.innerWidth - left);
        var height = Math.max(320, window.innerHeight - top);
        return { top: top, left: left, width: width, height: height };
    }

    function transformTableMath(table, doc) {
        var walker = doc.createTreeWalker(table, NodeFilter.SHOW_TEXT);
        var textNodes = [];
        var node;
        while ((node = walker.nextNode())) {
            if (!node.nodeValue || node.nodeValue.indexOf("$") === -1) continue;
            var p = node.parentElement;
            if (p && p.closest("code, pre, script, style, textarea, .language-math, .katex")) continue;
            textNodes.push(node);
        }
        textNodes.forEach(function (textNode) {
            var text = textNode.nodeValue;
            var combined = /\$\$([\s\S]+?)\$\$|(?<!\\)\$(?!\$)([^$\n]+?)(?<!\\)\$/g;
            var frag = doc.createDocumentFragment();
            var lastIndex = 0;
            var match;
            var changed = false;
            while ((match = combined.exec(text)) !== null) {
                changed = true;
                if (match.index > lastIndex) {
                    frag.appendChild(doc.createTextNode(text.slice(lastIndex, match.index)));
                }
                var tex = (match[1] !== undefined ? match[1] : match[2] || "").trim();
                var span = doc.createElement("span");
                span.className = "language-math";
                span.textContent = tex;
                frag.appendChild(span);
                lastIndex = combined.lastIndex;
            }
            if (!changed) return;
            if (lastIndex < text.length) {
                frag.appendChild(doc.createTextNode(text.slice(lastIndex)));
            }
            textNode.replaceWith(frag);
        });
    }

    function transformVditorPreviewHtml(htmlStr, svgCache) {
        var rewritten = rewritePreviewAssets(htmlStr || "");
        var tpl = document.createElement("template");
        tpl.innerHTML = rewritten;
        var theme = isDarkAdminTheme() ? "dark" : "default";

        // 1. 增强原生 HTML 表格单元格代码/图片预览，转换表格内公式为 KaTeX，并包裹宽表格滚动容器
        tpl.content.querySelectorAll("table").forEach(function (table) {
            transformTableRichContent(table, document);
            transformTableMath(table, document);
            if (table.parentElement && table.parentElement.classList.contains("cb-vditor-table-scroll")) return;
            var wrap = document.createElement("div");
            wrap.className = "cb-vditor-table-scroll";
            wrap.setAttribute("tabindex", "0");
            table.replaceWith(wrap);
            wrap.appendChild(table);
        });

        // 1b. 对所有预览图片应用 `alt|尺寸` 语法（如 |320, |50%, |sm|center）
        tpl.content.querySelectorAll("img").forEach(applyImageSizingToElement);

        // 2. 同时拦截 Lute sv 输出的 div.language-mermaid 与 pre > code.language-mermaid，
        //    注意：tpl.content 是游离 DocumentFragment，不能检查 node.isConnected（恒为 false），
        //    改用 node.parentNode 与 .cb-vditor-mermaid-host 祖先校验，彻底阻止 Vditor 拉取旧版 11.6.0。
        var mermaidNodes = Array.from(
            tpl.content.querySelectorAll("div.language-mermaid, pre > code.language-mermaid, .language-mermaid")
        );
        mermaidNodes.forEach(function (node) {
            if (!node.parentNode || (node.closest && node.closest(".cb-vditor-mermaid-host"))) return;
            var target = (node.tagName === "CODE" && node.parentElement && node.parentElement.tagName === "PRE")
                ? node.parentElement
                : node;
            if (!target.parentNode) return;
            var rawCode = (node.textContent || "").trim();
            var host = document.createElement("div");
            host.className = "cb-vditor-mermaid-host";
            host.setAttribute("data-cb-vditor-mermaid", rawCode);
            host.setAttribute("data-theme", theme);

            var cacheKey = theme + "::" + rawCode;
            if (svgCache && svgCache.has(cacheKey)) {
                host.innerHTML = svgCache.get(cacheKey);
                host.setAttribute("data-cb-mermaid-rendered-theme", theme);
            } else {
                var fallbackPre = document.createElement("pre");
                fallbackPre.className = "cb-vditor-mermaid-src";
                var fallbackCode = document.createElement("code");
                fallbackCode.textContent = rawCode;
                fallbackPre.appendChild(fallbackCode);
                host.appendChild(fallbackPre);
            }
            target.replaceWith(host);
        });

        return tpl.innerHTML;
    }

    class BoundVditorMarkdownWidget {
        constructor(root, name, parentCapabilities) {
            this.root = root;
            this.input = root.querySelector('textarea[name="' + name + '"]');
            if (!this.input) {
                throw new Error('No textarea found with name "' + name + '"');
            }
            this.editorEl = root.querySelector("[data-cb-vditor-editor]");
            this.statusEl = root.querySelector("[data-cb-vditor-status]");
            this.idForLabel = this.input.id;
            this.parentCapabilities = parentCapabilities || new Map();
            this.editor = null;
            this.ready = false;
            this.pendingValue = this.input.value || "";
            this._mermaidSvgCache = new Map();
            this._activeImgContext = null;
            this._imgToolbarEl = null;

            this._initVditor();
        }

        _setStatus(msg, isError) {
            if (!this.statusEl) return;
            if (!msg) {
                this.statusEl.hidden = true;
                this.statusEl.textContent = "";
                this.statusEl.classList.remove("is-error");
                return;
            }
            this.statusEl.hidden = false;
            this.statusEl.textContent = msg;
            this.statusEl.classList.toggle("is-error", Boolean(isError));
        }

        _syncToTextarea(val) {
            // Wagtail 自动保存会读取隐藏 textarea，不能让上传中临时占位符进入草稿版本
            var clean = stripTransientUploadTokens(val);
            if (this.input.value === clean) return;
            this.input.value = clean;
            dispatchTextareaEvents(this.input);
        }

        _replaceTokenInEditor(token, replacement) {
            var current = this.ready && this.editor ? this.editor.getValue() : this.input.value;
            var updated = current.replace(token, replacement);
            if (this.ready && this.editor && updated !== current) {
                this.editor.setValue(updated, true);
            }
            this._syncToTextarea(updated);
        }

        _uploadFiles(files) {
            var self = this;
            var uploadUrl = self.input.dataset.vditorUploadUrl;
            var maxSize = parseInt(self.input.dataset.vditorMaxSize || "10485760", 10);
            var form = self.input.closest("form");
            bindFormSubmitGuard(form);

            if (!uploadUrl || !files || files.length === 0) return null;

            Array.from(files).forEach(function (file) {
                if (!file) return;
                if (file.size > maxSize) {
                    self._setStatus("图片大小超过允许上限，已取消上传: " + (file.name || "image"), true);
                    return;
                }
                var id = Math.random().toString(36).slice(2, 10);
                var token = "<!--cb-vditor-upload:" + id + "-->";
                if (self.ready && self.editor) {
                    self.editor.insertValue("\n" + token + "\n");
                    self._syncToTextarea(self.editor.getValue());
                }

                var fd = new FormData();
                fd.append("file", file, file.name || "pasted-image.png");
                adjustPending(form, 1);
                self._setStatus("正在上传图片到作者受控图库...", false);

                fetch(uploadUrl, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: { "X-CSRFToken": getCsrfToken(self.root) },
                    body: fd
                })
                    .then(function (resp) {
                        return resp.json().then(function (data) {
                            return { ok: resp.ok, data: data };
                        });
                    })
                    .then(function (res) {
                        if (!res.ok || !res.data || !res.data.ok) {
                            var errMsg = (res.data && res.data.error && res.data.error.message) || "图片上传失败";
                            self._replaceTokenInEditor(token, "");
                            self._setStatus("上传失败: " + errMsg, true);
                            return;
                        }
                        self._replaceTokenInEditor(token, res.data.markdown);
                        self._setStatus("图片已上传并关联受控网关 (" + res.data.asset_id.slice(0, 8) + "...)", false);
                        setTimeout(function () { self._setStatus("", false); }, 3000);
                    })
                    .catch(function (err) {
                        self._replaceTokenInEditor(token, "");
                        self._setStatus("图片上传网络异常: " + (err.message || "请重试"), true);
                    })
                    .finally(function () {
                        adjustPending(form, -1);
                    });
            });

            return null;
        }

        _initVditor() {
            var self = this;
            if (typeof window.Vditor !== "function" || !this.editorEl) {
                return;
            }

            var cdn = this.input.dataset.vditorCdn || "/static/vendor/vditor";
            var mode = this.input.dataset.vditorMode || "sv";
            var lang = this.input.dataset.vditorLocale || "zh_CN";
            var isDark = isDarkAdminTheme();

            try {
                this.editor = new window.Vditor(this.editorEl, {
                    cdn: cdn,
                    mode: "sv",
                    lang: lang,
                    theme: isDark ? "dark" : "classic",
                    height: 460,
                    minHeight: 320,
                    value: this.pendingValue,
                    cache: { enable: false },
                    toolbarConfig: { pin: true },
                    toolbar: [
                        "headings",
                        "bold",
                        "italic",
                        "strike",
                        "|",
                        "list",
                        "ordered-list",
                        "check",
                        "quote",
                        "line",
                        "|",
                        "code",
                        "inline-code",
                        {
                            name: "cb-mermaid",
                            tipPosition: "n",
                            tip: "插入 Mermaid 流程图",
                            className: "cb-vditor-mermaid-btn",
                            icon: '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor"><path d="M2 2h4v3H2V2zm8 0h4v3h-4V2zM6 11h4v3H6v-3zM4 5v2.5A1.5 1.5 0 0 0 5.5 9h5A1.5 1.5 0 0 0 12 7.5V5h-1v2.5a.5.5 0 0 1-.5.5h-5a.5.5 0 0 1-.5-.5V5H4zm4 4v2h1V9H8z"/></svg>',
                            click: function () {
                                if (!self.ready || !self.editor) return;
                                self.editor.insertValue('\n```mermaid\nflowchart LR\n  A["开始"] --> B["处理"] --> C["完成"]\n```\n');
                                self._syncToTextarea(self.editor.getValue());
                                self._scheduleMermaidPreview();
                            }
                        },
                        {
                            name: "cb-complex-table",
                            tipPosition: "n",
                            tip: "插入复杂表格模板（含跨行跨列、公式、代码与图片）",
                            className: "cb-vditor-table-btn",
                            icon: '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor"><path d="M1 2a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V2zm1 3v3h4V5H2zm5 0v3h7V5H7zm-5 4v5h4V9H2zm5 0v5h7V9H7zM2 2v2h12V2H2z"/></svg>',
                            click: function () {
                                if (!self.ready || !self.editor) return;
                                // Vditor sv 的 insertValue 会把 HTML 转成 Markdown 而破坏合并单元格；
                                // 使用 text/plain paste（若选区在编辑器内）或 setValue 追加保留完整 HTML 复杂表格。
                                var tpl =
                                    '\n<table><thead>' +
                                    '<tr><th rowspan="2">模块</th><th colspan="3">支持能力演示</th></tr>' +
                                    '<tr><th>公式</th><th>代码高亮</th><th>插图（可用 |240 调尺寸）</th></tr>' +
                                    '</thead><tbody>' +
                                    '<tr><td rowspan="2">核心引擎</td>' +
                                    '<td>$E = mc^2$</td>' +
                                    '<td>\n```sql\nSELECT id, title FROM blog_blogpage;\n```\n</td>' +
                                    '<td>粘贴图片后可点右侧预览图快捷缩放（如 `![图注|240](asset://image/UUID)`）</td></tr>' +
                                    '<tr><td>$$\\sum_{i=1}^{n} x_i$$</td>' +
                                    '<td>`pip list`</td>' +
                                    '<td>支持 `|240`、`|320x180`、`|50%,center`</td></tr>' +
                                    '</tbody></table>\n';
                                var svEl = self.editorEl ? self.editorEl.querySelector(".vditor-sv") : null;
                                var beforeVal = self.editor.getValue();
                                var pasted = false;
                                if (svEl && window.DataTransfer && window.ClipboardEvent) {
                                    var sel = window.getSelection();
                                    if (sel && sel.rangeCount > 0 && svEl.contains(sel.anchorNode)) {
                                        var dt = new window.DataTransfer();
                                        dt.setData("text/plain", tpl);
                                        svEl.dispatchEvent(new window.ClipboardEvent("paste", {
                                            clipboardData: dt,
                                            bubbles: true,
                                            cancelable: true
                                        }));
                                        pasted = self.editor.getValue() !== beforeVal;
                                    }
                                }
                                if (!pasted) {
                                    var prefix = beforeVal && !beforeVal.endsWith("\n") ? "\n" : "";
                                    self.editor.setValue(beforeVal + prefix + tpl, true);
                                }
                                self._syncToTextarea(self.editor.getValue());
                            }
                        },
                        "link",
                        "table",
                        "|",
                        "undo",
                        "redo",
                        "|",
                        "fullscreen",
                        "preview",
                        "outline"
                    ],
                    preview: {
                        delay: 250,
                        markdown: {
                            breaks: true,
                            footnotes: true,
                            codeBlockPreview: true,
                            mathBlockPreview: true
                        },
                        hljs: { enable: true, style: isDark ? "github-dark" : "github", lineNumber: true },
                        math: { engine: "KaTeX", inlineDigit: true },
                        theme: {
                            current: isDark ? "dark" : "light",
                            path: cdn + "/dist/css/content-theme"
                        },
                        transform: function (htmlStr) {
                            return transformVditorPreviewHtml(htmlStr, self._mermaidSvgCache);
                        }
                    },
                    upload: {
                        accept: "image/*",
                        multiple: true,
                        handler: function (files) {
                            return self._uploadFiles(files);
                        }
                    },
                    input: function (value) {
                        self._syncToTextarea(value);
                        self._scheduleMermaidPreview();
                    },
                    blur: function (value) {
                        self._syncToTextarea(value);
                    },
                    after: function () {
                        self.ready = true;
                        self.root.classList.add("is-vditor-ready");
                        if (self.pendingValue !== self.editor.getValue()) {
                            self.editor.setValue(self.pendingValue, true);
                        }
                        self._initImageSizeToolbar();
                        self._observePreviewAndLayout();
                        self._scheduleMermaidPreview();
                    }
                });
            } catch (err) {
                console.warn("Vditor init fallback to plain textarea:", err);
            }
        }

        _initImageSizeToolbar() {
            var self = this;
            if (!this.editorEl || this._imgToolbarEl) return;
            var bar = document.createElement("div");
            bar.className = "cb-vditor-img-toolbar";
            bar.hidden = true;
            bar.innerHTML =
                '<span>调整选中图片尺寸：</span>' +
                '<button type="button" data-cb-img-spec="240">小图 240px</button>' +
                '<button type="button" data-cb-img-spec="480">中图 480px</button>' +
                '<button type="button" data-cb-img-spec="50%">50%</button>' +
                '<button type="button" data-cb-img-spec="75%">75%</button>' +
                '<button type="button" data-cb-img-spec="full">100%</button>' +
                '<button type="button" data-cb-img-spec="480,center">480px 居中</button>' +
                '<button type="button" data-cb-img-spec="">原图尺寸</button>' +
                '<button type="button" data-cb-img-close="1">关闭</button>';
            this.editorEl.insertAdjacentElement("afterend", bar);
            this._imgToolbarEl = bar;

            this.editorEl.addEventListener("click", function (e) {
                var img = e.target && e.target.closest ? e.target.closest(".vditor-preview img") : null;
                if (!img) return;
                var src = img.getAttribute("src") || "";
                var mGw = src.match(/\/media-gateway\/image\/([0-9a-fA-F-]{36})\/?/);
                var matchUrl = mGw ? ("asset://image/" + mGw[1]) : src;
                self._activeImgContext = {
                    matchUrl: matchUrl,
                    fallbackSrc: src
                };
                bar.hidden = false;
            });

            bar.addEventListener("click", function (e) {
                var closeBtn = e.target.closest("[data-cb-img-close]");
                if (closeBtn) {
                    bar.hidden = true;
                    self._activeImgContext = null;
                    return;
                }
                var btn = e.target.closest("[data-cb-img-spec]");
                if (!btn || !self._activeImgContext || !self.ready || !self.editor) return;
                var spec = btn.getAttribute("data-cb-img-spec") || "";
                self._applyImageResizeInMarkdown(self._activeImgContext, spec);
            });
        }

        _applyImageResizeInMarkdown(ctx, spec) {
            var md = this.editor.getValue();
            var urls = [ctx.matchUrl, ctx.fallbackSrc].filter(Boolean);
            var updated = md;
            var replaced = false;

            for (var i = 0; i < urls.length; i++) {
                var escapedUrl = urls[i].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                var re = new RegExp("!\\[([^\\]]*)\\]\\(" + escapedUrl + "\\)", "i");
                if (re.test(updated)) {
                    updated = updated.replace(re, function (_all, rawAlt) {
                        var parsed = parseImageAltAndAttrs(rawAlt);
                        var cleanAlt = parsed.attrs ? parsed.alt : rawAlt;
                        var newAlt = spec ? (cleanAlt ? (cleanAlt + "|" + spec) : ("|" + spec)) : cleanAlt;
                        return "![" + newAlt + "](" + urls[i] + ")";
                    });
                    replaced = true;
                    break;
                }
            }

            if (replaced && updated !== md) {
                this.editor.setValue(updated, true);
                this._syncToTextarea(updated);
                this._setStatus(spec ? ("已调整图片显示规格为: " + spec) : "已恢复图片原始尺寸", false);
                setTimeout(function () { self._setStatus("", false); }, 2500);
            } else {
                this._setStatus("未在 Markdown 源码中找到对应 ![...](...) 语法（若为原生 <img> 请直接修改 width 属性）", true);
            }
        }

        _observePreviewAndLayout() {
            var self = this;
            if (!window.MutationObserver || !this.editorEl) return;

            var previewObserver = new MutationObserver(function () {
                self._scheduleMermaidPreview();
                self._updateFullscreenBounds();
            });
            previewObserver.observe(this.editorEl, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });

            var themeObserver = new MutationObserver(function () {
                if (!self.editor || typeof self.editor.setTheme !== "function") return;
                var dark = isDarkAdminTheme();
                self.editor.setTheme(dark ? "dark" : "classic", dark ? "dark" : "light");
                self._renderMermaidHosts();
            });
            themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme"] });
            this._onWindowResize = function () {
                if (!self.root.isConnected) {
                    window.removeEventListener("resize", self._onWindowResize);
                    previewObserver.disconnect();
                    themeObserver.disconnect();
                    return;
                }
                self._updateFullscreenBounds();
            };
            window.addEventListener("resize", this._onWindowResize);
        }

        _updateFullscreenBounds() {
            if (!this.editorEl) return;
            var vditorRoot = this.editorEl.classList.contains("vditor") ? this.editorEl : this.editorEl.querySelector(".vditor");
            if (!vditorRoot) return;
            if (!vditorRoot.classList.contains("vditor--fullscreen")) {
                vditorRoot.style.removeProperty("--cb-vditor-fullscreen-top");
                vditorRoot.style.removeProperty("--cb-vditor-fullscreen-left");
                vditorRoot.style.removeProperty("--cb-vditor-fullscreen-width");
                vditorRoot.style.removeProperty("--cb-vditor-fullscreen-height");
                return;
            }
            var b = getAdminFullscreenBounds();
            vditorRoot.style.setProperty("--cb-vditor-fullscreen-top", b.top + "px");
            vditorRoot.style.setProperty("--cb-vditor-fullscreen-left", b.left + "px");
            vditorRoot.style.setProperty("--cb-vditor-fullscreen-width", b.width + "px");
            vditorRoot.style.setProperty("--cb-vditor-fullscreen-height", b.height + "px");
        }

        _scheduleMermaidPreview() {
            var self = this;
            if (this.mermaidTimer) clearTimeout(this.mermaidTimer);
            this.mermaidTimer = setTimeout(function () {
                self._renderMermaidHosts();
            }, 180);
        }

        _renderMermaidHosts() {
            var self = this;
            if (!this.editorEl || !window.CBMermaidRenderer) return;
            var theme = isDarkAdminTheme() ? "dark" : "default";
            var hosts = Array.from(this.editorEl.querySelectorAll(".cb-vditor-mermaid-host[data-cb-vditor-mermaid]")).filter(function (h) {
                return (
                    h.getAttribute("data-cb-mermaid-rendered-theme") !== theme &&
                    h.getAttribute("data-cb-mermaid-rendering-theme") !== theme
                );
            });
            if (hosts.length === 0) return;

            hosts.forEach(function (host) {
                var code = (host.getAttribute("data-cb-vditor-mermaid") || "").trim();
                if (!code) return;
                var cacheKey = theme + "::" + code;
                if (self._mermaidSvgCache.has(cacheKey)) {
                    host.setAttribute("data-theme", theme);
                    host.setAttribute("data-cb-mermaid-rendered-theme", theme);
                    host.removeAttribute("data-cb-mermaid-rendering-theme");
                    host.innerHTML = self._mermaidSvgCache.get(cacheKey);
                    return;
                }

                host.setAttribute("data-cb-mermaid-rendering-theme", theme);
                window.CBMermaidRenderer.render(code, theme).then(function (res) {
                    self._mermaidSvgCache.set(cacheKey, res.svgHtml);
                    if (!host.isConnected) return;
                    host.setAttribute("data-theme", res.theme);
                    host.setAttribute("data-cb-mermaid-rendered-theme", res.theme);
                    host.removeAttribute("data-cb-mermaid-rendering-theme");
                    host.innerHTML = "";
                    host.appendChild(res.svgNode);
                }).catch(function (err) {
                    if (!host.isConnected) return;
                    host.removeAttribute("data-cb-mermaid-rendering-theme");
                    var errEl = host.querySelector(".cb-vditor-mermaid-error");
                    if (!errEl) {
                        errEl = document.createElement("div");
                        errEl.className = "cb-vditor-mermaid-error";
                        host.appendChild(errEl);
                    }
                    errEl.textContent = "Mermaid 语法提示: " + (err.message || "请检查语法");
                });
            });
        }

        getValue() {
            if (this.ready && this.editor) {
                var val = stripTransientUploadTokens(this.editor.getValue());
                this.input.value = val;
                return val;
            }
            return stripTransientUploadTokens(this.input.value);
        }

        getState() {
            return this.getValue();
        }

        setState(val) {
            var strVal = val == null ? "" : String(val);
            this.pendingValue = strVal;
            this.input.value = strVal;
            if (this.ready && this.editor && this.editor.getValue() !== strVal) {
                this.editor.setValue(strVal, true);
            }
        }

        setInvalid(isInvalid) {
            if (isInvalid) {
                this.input.setAttribute("aria-invalid", "true");
            } else {
                this.input.removeAttribute("aria-invalid");
            }
        }

        getValueForLabel() {
            return this.getValue();
        }

        getTextLabel(opts) {
            var text = String(this.getValueForLabel() || "").trim();
            if (!text) return null;
            var maxLen = opts && opts.maxLength;
            return maxLen && text.length > maxLen ? text.substring(0, maxLen - 1) + "…" : text;
        }

        focus() {
            if (this.ready && this.editor && typeof this.editor.focus === "function") {
                this.editor.focus();
            } else {
                this.input.focus();
            }
        }

        setCapabilityOptions(capability, options) {
            var existing = this.parentCapabilities.get(capability);
            if (existing) Object.assign(existing, options);
        }
    }

    class VditorMarkdownWidgetAdapter {
        constructor(htmlTemplate) {
            this.html = htmlTemplate;
        }

        render(placeholder, name, id, initialState, parentCapabilities, options) {
            var renderedHtml = this.html.replace(/__NAME__/g, name).replace(/__ID__/g, id);
            var container = document.createElement("div");
            container.innerHTML = renderedHtml.trim();
            var root = container.firstElementChild;
            placeholder.replaceWith(root);
            if (options && options.attributes) {
                Object.keys(options.attributes).forEach(function (k) {
                    root.setAttribute(k, options.attributes[k]);
                });
            }
            var widget = new BoundVditorMarkdownWidget(root, name, parentCapabilities);
            widget.setState(initialState);
            return widget;
        }

        getByName(name, container) {
            var root = container.querySelector("[data-cb-vditor-field]") || container;
            return new BoundVditorMarkdownWidget(root, name);
        }
    }

    if (window.telepath && typeof window.telepath.register === "function") {
        window.telepath.register("content_blocks.widgets.VditorMarkdownWidget", VditorMarkdownWidgetAdapter);
    }
})(window, document);
