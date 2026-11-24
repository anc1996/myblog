/**
 * Wagtail 8.1 后台 Mermaid 增强分栏编辑器 (admin-mermaid-split.js)
 * 1. P0：7 类图表模板、8 类常用片段、行/字数统计、可靠行号定位与跳转、
 *        分栏/仅预览切换、错误保留上次成功结果、多块/排序/复制隔离与安全降级；
 * 2. P1：Pan/Zoom 画布（中心缩放、按指针滚轮缩放、拖拽与方向键平移、点阵网格）、
 *        可调分栏手柄、避开 Wagtail 顶栏/侧栏的工作区展开、安全 SVG 与离线 2× PNG 导出。
 */

(function (window, document) {
    "use strict";

    var THEME_LABELS = {
        "follow-site": "跟随站点昼夜 (follow-site)",
        default: "默认浅色 (default)",
        dark: "深色模式 (dark)",
        forest: "自然绿色 (forest)",
        neutral: "简约黑白 (neutral)"
    };

    var DIAGRAM_TEMPLATES = {
        flowchart: [
            "flowchart LR",
            '  A(["请求入口"]) --> B{"鉴权校验"}',
            '  B -- "通过" --> C["业务处理"]',
            '  B -- "拒绝" --> D["返回 403"]',
            '  C --> E[("PostgreSQL 主库")]'
        ].join("\n"),
        sequenceDiagram: [
            "sequenceDiagram",
            "  autonumber",
            "  actor U as 用户",
            "  participant W as Wagtail 后台",
            "  participant M as MongoDB 正文库",
            "  U->>W: 点击保存草稿",
            "  W->>M: 写入不可变正文版本",
            "  M-->>W: 返回版本指针",
            "  W-->>U: 提示保存成功"
        ].join("\n"),
        "stateDiagram-v2": [
            "stateDiagram-v2",
            "  [*] --> 草稿",
            "  草稿 --> 待审核: 提交审批",
            "  待审核 --> 已发布: 审核通过",
            "  待审核 --> 草稿: 退回修改",
            "  已发布 --> 回收站: 移入回收站",
            "  回收站 --> 草稿: 原位恢复"
        ].join("\n"),
        erDiagram: [
            "erDiagram",
            "  PAGE ||--o{ REVISION : has",
            "  PAGE {",
            "    int id PK",
            "    uuid document_uuid",
            "    bool is_recycled",
            "  }",
            "  REVISION {",
            "    int id PK",
            "    string content_pointer",
            "  }"
        ].join("\n"),
        classDiagram: [
            "classDiagram",
            "  class CustomBasePage {",
            "    +UUID document_uuid",
            "    +bool is_recycled",
            "    +save_revision()",
            "  }",
            "  class BlogPage {",
            "    +StreamField body",
            "    +extract_text()",
            "  }",
            "  CustomBasePage <|-- BlogPage"
        ].join("\n"),
        gantt: [
            "gantt",
            "  title 版本发布排期示例",
            "  dateFormat  YYYY-MM-DD",
            "  section 后端能力",
            "  搜索与索引       :done, s1, 2026-11-01, 5d",
            "  评论与治理       :active, s2, after s1, 4d",
            "  section 前端体验",
            "  编辑器升级       :s3, after s2, 3d"
        ].join("\n"),
        mindmap: [
            "mindmap",
            "  root((myblog 内容架构))",
            "    PostgreSQL",
            "      页面树与发布状态",
            "      GIN 全文检索",
            "    MongoDB",
            "      万字正文外置版本",
            "    RustFS",
            "      私有媒体网关交付"
        ].join("\n")
    };

    var SNIPPETS = {
        node_round: 'N1("圆角节点")',
        node_diamond: 'D1{"条件判断?"}',
        node_db: 'DB[("数据库")]',
        edge_label: 'A -- "说明文字" --> B',
        subgraph: 'subgraph 分组名称\n  direction TB\n  S1["步骤 1"] --> S2["步骤 2"]\nend',
        seq_msg: "A->>B: 请求消息",
        seq_alt: "alt 成功场景\n  B-->>A: 返回 200\nelse 异常场景\n  B-->>A: 返回错误\nend",
        state_trans: "状态A --> 状态B: 触发事件"
    };

    var widgetByRoot = new WeakMap();
    var expandedWidgets = new Set();

    function getExpandedWorkspaceBounds() {
        var vw = window.innerWidth || document.documentElement.clientWidth || 0;
        var vh = window.innerHeight || document.documentElement.clientHeight || 0;
        var main = document.getElementById("main");
        if (!main) {
            return { top: 0, left: 0, width: vw, height: vh };
        }
        var mainRect = main.getBoundingClientRect();
        var left = Math.max(0, Math.min(Math.round(mainRect.left), vw));
        var right = Math.max(left, Math.min(Math.round(mainRect.right), vw));
        var top = Math.max(0, Math.min(Math.round(mainRect.top), vh));
        var header = main.querySelector(".w-slim-header");
        if (header) {
            var hRect = header.getBoundingClientRect();
            if (hRect.height > 0 && hRect.bottom > top && hRect.top < vh) {
                top = Math.max(top, Math.min(Math.round(hRect.bottom), vh));
            }
        }
        return {
            top: top,
            left: left,
            width: Math.max(0, right - left),
            height: Math.max(0, vh - top)
        };
    }

    function clamp(val, min, max) {
        return Math.min(max, Math.max(min, val));
    }

    class BoundMermaidSplitWidget {
        constructor(root, name, parentCapabilities) {
            this.root = root;
            this.input = root.querySelector('textarea[name="' + name + '"]');
            if (!this.input) throw new Error('No textarea found with name "' + name + '"');

            this.canvas = root.querySelector("[data-cb-admin-mermaid-canvas]");
            this.stage = root.querySelector("[data-cb-mmd-stage]");
            this.errorEl = root.querySelector("[data-cb-admin-mermaid-error]");
            this.themeLabelEl = root.querySelector("[data-cb-admin-mermaid-theme-label]");
            this.gutterEl = root.querySelector("[data-cb-mmd-gutter]");
            this.gridEl = root.querySelector("[data-cb-mmd-grid]");
            this.splitterEl = root.querySelector("[data-cb-mmd-splitter]");

            this.tplSelect = root.querySelector("[data-cb-mmd-template-select]");
            this.snipSelect = root.querySelector("[data-cb-mmd-snippet-select]");
            this.indentBtn = root.querySelector("[data-cb-mmd-indent]");
            this.outdentBtn = root.querySelector("[data-cb-mmd-outdent]");
            this.tabToggleBtn = root.querySelector("[data-cb-mmd-tab-toggle]");
            this.resetSampleBtn = root.querySelector("[data-cb-mmd-reset-sample]");
            this.clearBtn = root.querySelector("[data-cb-mmd-clear]");
            this.copyBtn = root.querySelector("[data-cb-mmd-copy]");

            this.viewSplitBtn = root.querySelector("[data-cb-mmd-view-split]");
            this.viewPreviewBtn = root.querySelector("[data-cb-mmd-view-preview]");
            this.expandedThemeSel = root.querySelector("[data-cb-mmd-expanded-theme]");
            this.zoomOutBtn = root.querySelector("[data-cb-mmd-zoom-out]");
            this.zoomResetBtn = root.querySelector("[data-cb-mmd-zoom-reset]");
            this.zoomInBtn = root.querySelector("[data-cb-mmd-zoom-in]");
            this.zoomFitBtn = root.querySelector("[data-cb-mmd-zoom-fit]");
            this.wheelToggleBtn = root.querySelector("[data-cb-mmd-wheel-toggle]");
            this.gridToggleBtn = root.querySelector("[data-cb-mmd-grid-toggle]");
            this.exportSvgBtn = root.querySelector("[data-cb-mmd-export-svg]");
            this.exportPngBtn = root.querySelector("[data-cb-mmd-export-png]");
            this.expandBtn = root.querySelector("[data-cb-mmd-expand]");

            this.statsEl = root.querySelector("[data-cb-mmd-stats]");
            this.zoomLabelEl = root.querySelector("[data-cb-mmd-zoom-label]");
            this.statusPillEl = root.querySelector("[data-cb-mmd-status-pill]");
            this.jumpLineBtn = root.querySelector("[data-cb-mmd-jump-line]");

            this.idForLabel = this.input.id;
            this.parentCapabilities = parentCapabilities || new Map();
            this.debounceTimer = null;
            this.detachTimer = null;
            this.layoutRaf = null;
            this.latestReqId = 0;
            this.destroyed = false;

            this.tabIndentEnabled = false;
            this.wheelZoomEnabled = false;
            this.isPreviewOnly = false;
            this.isExpanded = false;
            this.leftRatioPct = 46;
            this.errorLine = null;
            this.lastSuccess = null;
            this.tempObjectUrls = new Set();

            this.viewState = {
                scale: 1,
                tx: 0,
                ty: 0,
                autoFit: true,
                baseWidth: 0,
                baseHeight: 0,
                dragging: false,
                pointerId: null,
                dragStartX: 0,
                dragStartY: 0,
                originTx: 0,
                originTy: 0
            };

            widgetByRoot.set(this.root, this);
            this._bindEvents();
            this._updateStatsAndGutter();
            this._scheduleRender();
        }

        _findThemeSelect() {
            var structContainer = this.root.closest(".struct-block");
            if (!structContainer) {
                var cp = this.root.closest("[data-contentpath]");
                structContainer = cp ? cp.parentElement : null;
            }
            if (structContainer) {
                var siblingSel = structContainer.querySelector('[data-contentpath="theme"] select, select[name$="-theme"]');
                if (siblingSel) return siblingSel;
            }
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
            var raw = (sel && sel.value) || "follow-site";
            return window.CBMermaidRenderer ? window.CBMermaidRenderer.normalizeTheme(raw) : raw;
        }

        _updateThemeVisuals(requestedTheme, resolvedTheme) {
            var visTheme = resolvedTheme || (window.CBMermaidRenderer && window.CBMermaidRenderer.resolveTheme ? window.CBMermaidRenderer.resolveTheme(requestedTheme) : requestedTheme);
            if (this.canvas) {
                this.canvas.setAttribute("data-theme", visTheme);
            }
            if (this.themeLabelEl) {
                this.themeLabelEl.textContent = "主题: " + (THEME_LABELS[requestedTheme] || requestedTheme);
                this.themeLabelEl.setAttribute("data-theme", visTheme);
            }
            if (this.expandedThemeSel && this.expandedThemeSel.value !== requestedTheme) {
                this.expandedThemeSel.value = requestedTheme;
            }
        }

        _setStatusPill(text, state) {
            if (!this.statusPillEl) return;
            this.statusPillEl.textContent = text;
            if (state) {
                this.statusPillEl.setAttribute("data-state", state);
            } else {
                this.statusPillEl.removeAttribute("data-state");
            }
        }

        _syncExportButtons() {
            var currentCode = this.input.value || "";
            var currentTheme = this._getTheme();
            var ready = Boolean(
                this.lastSuccess &&
                this.lastSuccess.svgNode &&
                this.lastSuccess.code === currentCode &&
                (this.lastSuccess.requestedTheme || this.lastSuccess.theme) === currentTheme
            );
            if (this.exportSvgBtn) this.exportSvgBtn.disabled = !ready;
            if (this.exportPngBtn) this.exportPngBtn.disabled = !ready;
        }

        _updateStatsAndGutter() {
            var val = this.input.value || "";
            var normalized = val.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
            var lineCount = normalized ? normalized.split("\n").length : 1;
            var charCount = val.length;
            var maxChars = (window.CBMermaidRenderer && window.CBMermaidRenderer.MAX_SOURCE_CHARS) || 50000;

            if (this.statsEl) {
                this.statsEl.textContent = lineCount + " 行 · " + charCount + " / " + maxChars + " 字符";
                this.statsEl.style.color = charCount > maxChars ? "#be123c" : "";
            }

            if (this.gutterEl) {
                var frag = document.createDocumentFragment();
                for (var i = 1; i <= lineCount; i++) {
                    var div = document.createElement("div");
                    div.className = "cb-mmd-gutter-line" + (this.errorLine === i ? " is-error-line" : "");
                    div.textContent = String(i);
                    frag.appendChild(div);
                }
                this.gutterEl.innerHTML = "";
                this.gutterEl.appendChild(frag);
                this.gutterEl.scrollTop = this.input.scrollTop;
            }
        }

        _replaceTextareaRange(start, end, replacement, selectMode) {
            var input = this.input;
            input.focus();
            input.setSelectionRange(start, end);
            var before = input.value;
            var execOk = false;
            try {
                execOk = document.execCommand && document.execCommand("insertText", false, replacement);
            } catch (_e) {
                execOk = false;
            }
            if (!execOk || input.value === before && replacement !== before.slice(start, end)) {
                input.setRangeText(replacement, start, end, selectMode || "end");
                input.dispatchEvent(new Event("input", { bubbles: true }));
                input.dispatchEvent(new Event("change", { bubbles: true }));
            } else {
                input.dispatchEvent(new Event("change", { bubbles: true }));
            }
            if (selectMode === "select") {
                input.setSelectionRange(start, start + replacement.length);
            }
        }

        _indentSelectedLines(isOutdent) {
            var val = this.input.value || "";
            var selStart = this.input.selectionStart || 0;
            var selEnd = this.input.selectionEnd || 0;
            var lineStart = val.lastIndexOf("\n", Math.max(0, selStart - 1)) + 1;
            var lineEndIdx = val.indexOf("\n", selEnd);
            var lineEnd = lineEndIdx === -1 ? val.length : lineEndIdx;
            var block = val.slice(lineStart, lineEnd);
            var lines = block.split("\n");
            var transformed = lines.map(function (line) {
                if (isOutdent) {
                    return line.replace(/^(?: {1,2}|\t)/, "");
                }
                return "  " + line;
            }).join("\n");
            this._replaceTextareaRange(lineStart, lineEnd, transformed, "select");
        }

        _jumpToLine(lineNum) {
            if (!lineNum || lineNum < 1) return;
            if (this.isPreviewOnly) {
                this._setViewMode("split");
            }
            var val = this.input.value || "";
            var lines = val.split(/\r?\n/);
            var targetIdx = Math.min(lineNum - 1, Math.max(0, lines.length - 1));
            var pos = 0;
            for (var i = 0; i < targetIdx; i++) {
                pos += lines[i].length + 1;
            }
            var endPos = pos + (lines[targetIdx] ? lines[targetIdx].length : 0);
            this.input.focus();
            this.input.setSelectionRange(pos, endPos);
            var lineHeight = parseFloat(window.getComputedStyle(this.input).lineHeight) || 20;
            this.input.scrollTop = Math.max(0, targetIdx * lineHeight - this.input.clientHeight / 3);
            if (this.gutterEl) this.gutterEl.scrollTop = this.input.scrollTop;
        }

        _setViewMode(mode) {
            this.isPreviewOnly = mode === "preview";
            this.root.classList.toggle("is-preview-only", this.isPreviewOnly);
            if (this.viewSplitBtn) {
                this.viewSplitBtn.classList.toggle("is-active", !this.isPreviewOnly);
                this.viewSplitBtn.setAttribute("aria-pressed", String(!this.isPreviewOnly));
            }
            if (this.viewPreviewBtn) {
                this.viewPreviewBtn.classList.toggle("is-active", this.isPreviewOnly);
                this.viewPreviewBtn.setAttribute("aria-pressed", String(this.isPreviewOnly));
            }
            this._scheduleLayoutSync();
        }

        _applyTransform() {
            if (!this.stage) return;
            if (!this.viewState.baseWidth) {
                this.stage.style.transform = "";
                if (this.zoomLabelEl) this.zoomLabelEl.textContent = "100%";
                return;
            }
            this.stage.style.transform =
                "translate(" + Math.round(this.viewState.tx) + "px, " + Math.round(this.viewState.ty) + "px) " +
                "scale(" + this.viewState.scale + ")";
            if (this.zoomLabelEl) {
                this.zoomLabelEl.textContent = Math.round(this.viewState.scale * 100) + "%";
            }
        }

        _measureSvgBaseSize(svg) {
            if (!svg) return { width: 0, height: 0 };
            var vb = svg.viewBox && svg.viewBox.baseVal;
            if (vb && vb.width > 0 && vb.height > 0) {
                return { width: vb.width, height: vb.height };
            }
            var rect = svg.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                return { width: rect.width / (this.viewState.scale || 1), height: rect.height / (this.viewState.scale || 1) };
            }
            return { width: 480, height: 240 };
        }

        _fitToViewport(enableAutoFit) {
            if (!this.canvas || !this.viewState.baseWidth || !this.viewState.baseHeight) return;
            var vw = this.canvas.clientWidth;
            var vh = this.canvas.clientHeight;
            if (vw <= 0 || vh <= 0) return;
            var pad = 28;
            var scaleX = (vw - pad) / this.viewState.baseWidth;
            var scaleY = (vh - pad) / this.viewState.baseHeight;
            var fitScale = clamp(Math.min(1, scaleX, scaleY), 0.15, 3);
            this.viewState.scale = fitScale;
            this.viewState.tx = (vw - this.viewState.baseWidth * fitScale) / 2;
            this.viewState.ty = (vh - this.viewState.baseHeight * fitScale) / 2;
            if (enableAutoFit !== undefined) this.viewState.autoFit = Boolean(enableAutoFit);
            this._applyTransform();
        }

        _resetZoom100() {
            if (!this.canvas || !this.viewState.baseWidth) return;
            var vw = this.canvas.clientWidth || 400;
            var vh = this.canvas.clientHeight || 300;
            this.viewState.autoFit = false;
            this.viewState.scale = 1;
            this.viewState.tx = (vw - this.viewState.baseWidth) / 2;
            this.viewState.ty = (vh - this.viewState.baseHeight) / 2;
            this._applyTransform();
        }

        _zoomAt(factor, anchorX, anchorY) {
            if (!this.canvas || !this.viewState.baseWidth) return;
            var oldScale = this.viewState.scale || 1;
            var newScale = clamp(oldScale * factor, 0.15, 4);
            if (newScale === oldScale) return;
            var ratio = newScale / oldScale;
            this.viewState.autoFit = false;
            this.viewState.tx = anchorX - (anchorX - this.viewState.tx) * ratio;
            this.viewState.ty = anchorY - (anchorY - this.viewState.ty) * ratio;
            this.viewState.scale = newScale;
            this._applyTransform();
        }

        _panBy(dx, dy) {
            if (!this.viewState.baseWidth) return;
            this.viewState.autoFit = false;
            this.viewState.tx += dx;
            this.viewState.ty += dy;
            this._applyTransform();
        }

        _setExpanded(enabled) {
            var target = Boolean(enabled);
            if (target === this.isExpanded) return;
            if (target) {
                var bounds = getExpandedWorkspaceBounds();
                if (bounds.width < 360 || bounds.height < 260) {
                    this._setStatusPill("当前窗口空间不足，保持内嵌编辑", "error");
                    return;
                }
                Array.from(expandedWidgets).forEach(function (other) {
                    if (other !== this && typeof other._setExpanded === "function") {
                        other._setExpanded(false);
                    }
                }, this);
                this.isExpanded = true;
                expandedWidgets.add(this);
                this.root.classList.add("is-expanded");
                document.body.classList.add("cb-mmd-expanded-active");
                this._applyExpandedBounds(bounds);
                if (this.expandedThemeSel) this.expandedThemeSel.hidden = false;
                if (this.expandBtn) {
                    this.expandBtn.setAttribute("aria-pressed", "true");
                    this.expandBtn.textContent = "✕ 收起";
                }
            } else {
                this.isExpanded = false;
                expandedWidgets.delete(this);
                this.root.classList.remove("is-expanded");
                if (expandedWidgets.size === 0) {
                    document.body.classList.remove("cb-mmd-expanded-active");
                }
                this.root.style.removeProperty("--cb-mmd-exp-top");
                this.root.style.removeProperty("--cb-mmd-exp-left");
                this.root.style.removeProperty("--cb-mmd-exp-width");
                this.root.style.removeProperty("--cb-mmd-exp-height");
                if (this.expandedThemeSel) this.expandedThemeSel.hidden = true;
                if (this.expandBtn) {
                    this.expandBtn.setAttribute("aria-pressed", "false");
                    this.expandBtn.textContent = "⛶ 展开";
                }
            }
            this._scheduleLayoutSync();
        }

        _applyExpandedBounds(precomputed) {
            if (!this.isExpanded || !this.root.isConnected) return;
            var b = precomputed || getExpandedWorkspaceBounds();
            if (b.width < 360 || b.height < 260) {
                this._setExpanded(false);
                return;
            }
            this.root.style.setProperty("--cb-mmd-exp-top", b.top + "px");
            this.root.style.setProperty("--cb-mmd-exp-left", b.left + "px");
            this.root.style.setProperty("--cb-mmd-exp-width", b.width + "px");
            this.root.style.setProperty("--cb-mmd-exp-height", b.height + "px");
        }

        _scheduleLayoutSync() {
            var self = this;
            if (this.layoutRaf !== null || typeof window.requestAnimationFrame !== "function") {
                if (this.isExpanded) this._applyExpandedBounds();
                if (this.viewState.autoFit) this._fitToViewport(true);
                return;
            }
            this.layoutRaf = window.requestAnimationFrame(function () {
                self.layoutRaf = null;
                if (self.destroyed || !self.root.isConnected) return;
                if (self.isExpanded) self._applyExpandedBounds();
                if (self.viewState.autoFit) self._fitToViewport(true);
            });
        }

        _triggerDownload(blob, filename) {
            var url = URL.createObjectURL(blob);
            this.tempObjectUrls.add(url);
            var a = document.createElement("a");
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            var self = this;
            setTimeout(function () {
                URL.revokeObjectURL(url);
                self.tempObjectUrls.delete(url);
            }, 1500);
        }

        _exportSvg() {
            if (!this.lastSuccess || !this.lastSuccess.svgNode || !window.CBMermaidRenderer) return;
            if (this.lastSuccess.code !== (this.input.value || "") || (this.lastSuccess.requestedTheme || this.lastSuccess.theme) !== this._getTheme()) {
                this._setStatusPill("源码已变更，请等待最新渲染完成后再导出", "error");
                return;
            }
            var svgStr = window.CBMermaidRenderer.serializeSafeSvg(this.lastSuccess.svgNode);
            if (!svgStr) {
                this._setStatusPill("SVG 净化导出失败", "error");
                return;
            }
            var blob = new Blob([svgStr], { type: "image/svg+xml;charset=utf-8" });
            this._triggerDownload(blob, "mermaid-diagram.svg");
        }

        _exportPng() {
            var self = this;
            if (!this.lastSuccess || !this.lastSuccess.svgNode || !window.CBMermaidRenderer) return;
            if (this.lastSuccess.code !== (this.input.value || "") || (this.lastSuccess.requestedTheme || this.lastSuccess.theme) !== this._getTheme()) {
                this._setStatusPill("源码已变更，请等待最新渲染完成后再导出", "error");
                return;
            }
            var clone = window.CBMermaidRenderer.cloneSanitizedSvg(this.lastSuccess.svgNode);
            if (!clone) {
                this._setStatusPill("PNG 导出前 SVG 校验失败", "error");
                return;
            }
            var w = Math.max(1, Math.ceil(this.viewState.baseWidth || 800));
            var h = Math.max(1, Math.ceil(this.viewState.baseHeight || 400));
            clone.setAttribute("width", String(w));
            clone.setAttribute("height", String(h));
            var svgStr = '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(clone);
            var svgBlob = new Blob([svgStr], { type: "image/svg+xml;charset=utf-8" });
            var svgUrl = URL.createObjectURL(svgBlob);
            this.tempObjectUrls.add(svgUrl);

            var scale = 2;
            var maxSide = 4096;
            var maxPixels = 16000000;
            if (w * scale > maxSide || h * scale > maxSide) {
                scale = Math.min(scale, maxSide / Math.max(w, h));
            }
            if (w * scale * h * scale > maxPixels) {
                scale = Math.min(scale, Math.sqrt(maxPixels / (w * h)));
            }
            scale = Math.max(0.5, scale);

            var img = new Image();
            img.onload = function () {
                URL.revokeObjectURL(svgUrl);
                self.tempObjectUrls.delete(svgUrl);
                if (self.destroyed) return;
                try {
                    var cw = Math.max(1, Math.round(w * scale));
                    var ch = Math.max(1, Math.round(h * scale));
                    var canvas = document.createElement("canvas");
                    canvas.width = cw;
                    canvas.height = ch;
                    var ctx = canvas.getContext("2d");
                    if (!ctx) {
                        self._setStatusPill("浏览器不支持 Canvas 2D，请改用 SVG 下载", "error");
                        return;
                    }
                    ctx.fillStyle = self.lastSuccess.theme === "dark" ? "#1f2430" : "#ffffff";
                    ctx.fillRect(0, 0, cw, ch);
                    ctx.drawImage(img, 0, 0, cw, ch);
                    canvas.toBlob(function (pngBlob) {
                        if (self.destroyed) return;
                        if (!pngBlob) {
                            self._setStatusPill("PNG 编码失败，请改用 SVG 下载", "error");
                            return;
                        }
                        self._triggerDownload(pngBlob, "mermaid-diagram.png");
                    }, "image/png");
                } catch (_err) {
                    self._setStatusPill("浏览器安全策略阻止 PNG 导出，请使用 SVG 下载", "error");
                }
            };
            img.onerror = function () {
                URL.revokeObjectURL(svgUrl);
                self.tempObjectUrls.delete(svgUrl);
                if (!self.destroyed) {
                    self._setStatusPill("SVG 解码为 PNG 失败，请改用 SVG 下载", "error");
                }
            };
            img.src = svgUrl;
        }

        _bindEvents() {
            var self = this;

            this.input.addEventListener("input", function () {
                self.latestReqId++;
                self._updateStatsAndGutter();
                self._syncExportButtons();
                self._setStatusPill("待更新", "pending");
                self._scheduleRender();
            });
            this.input.addEventListener("scroll", function () {
                if (self.gutterEl) self.gutterEl.scrollTop = self.input.scrollTop;
            });
            this.input.addEventListener("keydown", function (e) {
                if (!self.tabIndentEnabled) return;
                if (e.key === "Tab") {
                    e.preventDefault();
                    self._indentSelectedLines(e.shiftKey);
                } else if (e.key === "Escape") {
                    e.preventDefault();
                    self.tabIndentEnabled = false;
                    if (self.tabToggleBtn) self.tabToggleBtn.setAttribute("aria-pressed", "false");
                    self._setStatusPill("已退出 Tab 缩进模式", "");
                }
            });

            if (this.tplSelect) {
                this.tplSelect.addEventListener("change", function () {
                    var key = self.tplSelect.value;
                    self.tplSelect.value = "";
                    if (!key || !DIAGRAM_TEMPLATES[key]) return;
                    var cur = self.input.value || "";
                    if (cur.trim() && !window.confirm("当前编辑区已有源码，确定替换为所选模板吗？（可用 Ctrl+Z 撤销）")) {
                        return;
                    }
                    self._replaceTextareaRange(0, cur.length, DIAGRAM_TEMPLATES[key], "end");
                });
            }

            if (this.snipSelect) {
                this.snipSelect.addEventListener("change", function () {
                    var key = self.snipSelect.value;
                    self.snipSelect.value = "";
                    if (!key || !SNIPPETS[key]) return;
                    var start = typeof self.input.selectionStart === "number" ? self.input.selectionStart : self.input.value.length;
                    var end = typeof self.input.selectionEnd === "number" ? self.input.selectionEnd : start;
                    var prefix = start > 0 && self.input.value[start - 1] !== "\n" ? "\n" : "";
                    self._replaceTextareaRange(start, end, prefix + SNIPPETS[key], "end");
                });
            }

            if (this.indentBtn) this.indentBtn.addEventListener("click", function () { self._indentSelectedLines(false); });
            if (this.outdentBtn) this.outdentBtn.addEventListener("click", function () { self._indentSelectedLines(true); });
            if (this.tabToggleBtn) {
                this.tabToggleBtn.addEventListener("click", function () {
                    self.tabIndentEnabled = !self.tabIndentEnabled;
                    self.tabToggleBtn.setAttribute("aria-pressed", String(self.tabIndentEnabled));
                    self.input.focus();
                });
            }
            if (this.resetSampleBtn) {
                this.resetSampleBtn.addEventListener("click", function () {
                    var cur = self.input.value || "";
                    if (cur.trim() && !window.confirm("确定恢复为默认流程图示例吗？（可用 Ctrl+Z 撤销）")) return;
                    self._replaceTextareaRange(0, cur.length, DIAGRAM_TEMPLATES.flowchart, "end");
                });
            }
            if (this.clearBtn) {
                this.clearBtn.addEventListener("click", function () {
                    var cur = self.input.value || "";
                    if (!cur) return;
                    if (!window.confirm("确定清空当前 Mermaid 源码吗？（可用 Ctrl+Z 撤销）")) return;
                    self._replaceTextareaRange(0, cur.length, "", "end");
                });
            }
            if (this.copyBtn) {
                this.copyBtn.addEventListener("click", function () {
                    var text = self.input.value || "";
                    var done = function () {
                        var old = self.copyBtn.textContent;
                        self.copyBtn.textContent = "已复制 ✓";
                        setTimeout(function () { if (!self.destroyed) self.copyBtn.textContent = old; }, 1600);
                    };
                    if (navigator.clipboard && window.isSecureContext) {
                        navigator.clipboard.writeText(text).then(done).catch(function () {});
                    } else {
                        var ta = document.createElement("textarea");
                        ta.value = text;
                        ta.style.position = "fixed";
                        ta.style.left = "-9999px";
                        document.body.appendChild(ta);
                        ta.select();
                        try { if (document.execCommand("copy")) done(); } finally { ta.remove(); }
                    }
                });
            }

            if (this.viewSplitBtn) this.viewSplitBtn.addEventListener("click", function () { self._setViewMode("split"); });
            if (this.viewPreviewBtn) this.viewPreviewBtn.addEventListener("click", function () { self._setViewMode("preview"); });

            if (this.expandedThemeSel) {
                this.expandedThemeSel.addEventListener("change", function () {
                    var themeSel = self._findThemeSelect();
                    if (themeSel && themeSel.value !== self.expandedThemeSel.value) {
                        themeSel.value = self.expandedThemeSel.value;
                        themeSel.dispatchEvent(new Event("change", { bubbles: true }));
                    }
                });
            }

            if (this.zoomInBtn) {
                this.zoomInBtn.addEventListener("click", function () {
                    var vw = (self.canvas && self.canvas.clientWidth) || 400;
                    var vh = (self.canvas && self.canvas.clientHeight) || 300;
                    self._zoomAt(1.2, vw / 2, vh / 2);
                });
            }
            if (this.zoomOutBtn) {
                this.zoomOutBtn.addEventListener("click", function () {
                    var vw = (self.canvas && self.canvas.clientWidth) || 400;
                    var vh = (self.canvas && self.canvas.clientHeight) || 300;
                    self._zoomAt(1 / 1.2, vw / 2, vh / 2);
                });
            }
            if (this.zoomResetBtn) this.zoomResetBtn.addEventListener("click", function () { self._resetZoom100(); });
            if (this.zoomFitBtn) this.zoomFitBtn.addEventListener("click", function () { self._fitToViewport(true); });

            if (this.wheelToggleBtn) {
                this.wheelToggleBtn.addEventListener("click", function () {
                    self.wheelZoomEnabled = !self.wheelZoomEnabled;
                    self.wheelToggleBtn.setAttribute("aria-pressed", String(self.wheelZoomEnabled));
                });
            }
            if (this.gridToggleBtn) {
                this.gridToggleBtn.addEventListener("click", function () {
                    if (!self.canvas) return;
                    var next = self.canvas.classList.toggle("has-grid");
                    self.gridToggleBtn.setAttribute("aria-pressed", String(next));
                });
            }
            if (this.exportSvgBtn) this.exportSvgBtn.addEventListener("click", function () { self._exportSvg(); });
            if (this.exportPngBtn) this.exportPngBtn.addEventListener("click", function () { self._exportPng(); });
            if (this.expandBtn) this.expandBtn.addEventListener("click", function () { self._setExpanded(!self.isExpanded); });
            if (this.jumpLineBtn) this.jumpLineBtn.addEventListener("click", function () { self._jumpToLine(self.errorLine); });

            this.root.querySelectorAll("[data-cb-mmd-pan]").forEach(function (btn) {
                btn.addEventListener("click", function () {
                    var dir = btn.getAttribute("data-cb-mmd-pan");
                    if (dir === "left") self._panBy(-48, 0);
                    else if (dir === "right") self._panBy(48, 0);
                    else if (dir === "up") self._panBy(0, -48);
                    else if (dir === "down") self._panBy(0, 48);
                });
            });

            if (this.canvas) {
                this.canvas.addEventListener("pointerdown", function (e) {
                    if (e.button !== 0 || (e.target && e.target.closest && e.target.closest("button"))) return;
                    self.viewState.dragging = true;
                    self.viewState.pointerId = e.pointerId;
                    self.viewState.dragStartX = e.clientX;
                    self.viewState.dragStartY = e.clientY;
                    self.viewState.originTx = self.viewState.tx;
                    self.viewState.originTy = self.viewState.ty;
                    self.canvas.classList.add("is-panning");
                    if (self.canvas.setPointerCapture) {
                        try { self.canvas.setPointerCapture(e.pointerId); } catch (_err) {}
                    }
                });
                this.canvas.addEventListener("pointermove", function (e) {
                    if (!self.viewState.dragging || e.pointerId !== self.viewState.pointerId) return;
                    self.viewState.autoFit = false;
                    self.viewState.tx = self.viewState.originTx + (e.clientX - self.viewState.dragStartX);
                    self.viewState.ty = self.viewState.originTy + (e.clientY - self.viewState.dragStartY);
                    self._applyTransform();
                });
                var stopPan = function (e) {
                    if (!self.viewState.dragging) return;
                    if (e && e.pointerId !== undefined && e.pointerId !== self.viewState.pointerId) return;
                    self.viewState.dragging = false;
                    self.canvas.classList.remove("is-panning");
                    if (e && e.pointerId !== undefined && self.canvas.releasePointerCapture) {
                        try { self.canvas.releasePointerCapture(e.pointerId); } catch (_err) {}
                    }
                    self.viewState.pointerId = null;
                };
                this.canvas.addEventListener("pointerup", stopPan);
                this.canvas.addEventListener("pointercancel", stopPan);
                this.canvas.addEventListener("lostpointercapture", stopPan);
                this.canvas.addEventListener("blur", function () { stopPan(); });
                this.canvas.addEventListener("wheel", function (e) {
                    var active = self.wheelZoomEnabled || (e.ctrlKey && document.activeElement === self.canvas);
                    if (!active || !self.viewState.baseWidth) return;
                    e.preventDefault();
                    var rect = self.canvas.getBoundingClientRect();
                    var factor = e.deltaY < 0 ? 1.12 : (1 / 1.12);
                    self._zoomAt(factor, e.clientX - rect.left, e.clientY - rect.top);
                }, { passive: false });
                this.canvas.addEventListener("keydown", function (e) {
                    if (e.target !== self.canvas) return;
                    if (e.key === "ArrowLeft") { e.preventDefault(); self._panBy(-40, 0); }
                    else if (e.key === "ArrowRight") { e.preventDefault(); self._panBy(40, 0); }
                    else if (e.key === "ArrowUp") { e.preventDefault(); self._panBy(0, -40); }
                    else if (e.key === "ArrowDown") { e.preventDefault(); self._panBy(0, 40); }
                });
            }

            if (this.splitterEl && this.gridEl) {
                var dragSplit = { active: false, pointerId: null };
                this.splitterEl.addEventListener("pointerdown", function (e) {
                    if (e.button !== 0) return;
                    dragSplit.active = true;
                    dragSplit.pointerId = e.pointerId;
                    self.splitterEl.classList.add("is-dragging");
                    if (self.splitterEl.setPointerCapture) {
                        try { self.splitterEl.setPointerCapture(e.pointerId); } catch (_err) {}
                    }
                });
                this.splitterEl.addEventListener("pointermove", function (e) {
                    if (!dragSplit.active || e.pointerId !== dragSplit.pointerId) return;
                    var rect = self.gridEl.getBoundingClientRect();
                    if (rect.width <= 0) return;
                    var pct = clamp(Math.round(((e.clientX - rect.left) / rect.width) * 100), 25, 75);
                    self.leftRatioPct = pct;
                    self.gridEl.style.setProperty("--cb-mmd-left-pct", pct + "%");
                    self.splitterEl.setAttribute("aria-valuenow", String(pct));
                    self._scheduleLayoutSync();
                });
                var endSplit = function () {
                    if (!dragSplit.active) return;
                    dragSplit.active = false;
                    self.splitterEl.classList.remove("is-dragging");
                    dragSplit.pointerId = null;
                };
                this.splitterEl.addEventListener("pointerup", endSplit);
                this.splitterEl.addEventListener("pointercancel", endSplit);
                this.splitterEl.addEventListener("lostpointercapture", endSplit);
                this.splitterEl.addEventListener("keydown", function (e) {
                    if (e.key === "ArrowLeft") {
                        e.preventDefault();
                        self.leftRatioPct = clamp(self.leftRatioPct - 3, 25, 75);
                    } else if (e.key === "ArrowRight") {
                        e.preventDefault();
                        self.leftRatioPct = clamp(self.leftRatioPct + 3, 25, 75);
                    } else {
                        return;
                    }
                    self.gridEl.style.setProperty("--cb-mmd-left-pct", self.leftRatioPct + "%");
                    self.splitterEl.setAttribute("aria-valuenow", String(self.leftRatioPct));
                    self._scheduleLayoutSync();
                });
            }

            this._onDocThemeChange = function (e) {
                if (self.destroyed) return;
                var target = e.target;
                if (!target || target.tagName !== "SELECT") return;
                var themeSel = self._findThemeSelect();
                if (target === themeSel) {
                    if (self.debounceTimer) {
                        clearTimeout(self.debounceTimer);
                        self.debounceTimer = null;
                    }
                    self.latestReqId++;
                    self._syncExportButtons();
                    self._renderPreview();
                }
            };
            document.addEventListener("change", this._onDocThemeChange);
            document.addEventListener("input", this._onDocThemeChange);

            this._onDocEsc = function (e) {
                if (e.key !== "Escape" || !self.isExpanded || self.destroyed) return;
                if (document.activeElement && document.activeElement.tagName === "SELECT") return;
                e.preventDefault();
                self._setExpanded(false);
                if (self.expandBtn) self.expandBtn.focus();
            };
            document.addEventListener("keydown", this._onDocEsc);

            this._onWindowLayout = function () { self._scheduleLayoutSync(); };
            window.addEventListener("resize", this._onWindowLayout);
            window.addEventListener("scroll", this._onWindowLayout, { passive: true });

            if (window.ResizeObserver && this.canvas) {
                this._resizeObserver = new window.ResizeObserver(function () {
                    self._scheduleLayoutSync();
                });
                this._resizeObserver.observe(this.canvas);
                var mainEl = document.getElementById("main");
                if (mainEl) this._resizeObserver.observe(mainEl);
            }

            if (window.MutationObserver && document.body) {
                this._lifecycleObserver = new window.MutationObserver(function () {
                    if (self.destroyed) return;
                    if (!self.root.isConnected) {
                        if (!self.detachTimer) {
                            self.detachTimer = setTimeout(function () {
                                self.detachTimer = null;
                                if (!self.root.isConnected) {
                                    self.destroy();
                                }
                            }, 180);
                        }
                    } else if (self.detachTimer) {
                        clearTimeout(self.detachTimer);
                        self.detachTimer = null;
                    }
                });
                this._lifecycleObserver.observe(document.body, { childList: true, subtree: true });
            }
        }

        destroy() {
            if (this.destroyed) return;
            this.destroyed = true;
            if (this.isExpanded) this._setExpanded(false);
            if (this.debounceTimer) clearTimeout(this.debounceTimer);
            if (this.detachTimer) clearTimeout(this.detachTimer);
            if (this.layoutRaf !== null && typeof window.cancelAnimationFrame === "function") {
                window.cancelAnimationFrame(this.layoutRaf);
            }
            document.removeEventListener("change", this._onDocThemeChange);
            document.removeEventListener("input", this._onDocThemeChange);
            document.removeEventListener("keydown", this._onDocEsc);
            window.removeEventListener("resize", this._onWindowLayout);
            window.removeEventListener("scroll", this._onWindowLayout);
            if (this._resizeObserver) this._resizeObserver.disconnect();
            if (this._lifecycleObserver) this._lifecycleObserver.disconnect();
            this.tempObjectUrls.forEach(function (u) { URL.revokeObjectURL(u); });
            this.tempObjectUrls.clear();
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
            if (this.destroyed) return;
            var rawCode = this.input.value || "";
            var code = rawCode.trim();
            var theme = this._getTheme();
            this._updateThemeVisuals(theme);
            var reqId = ++this.latestReqId;

            if (!code) {
                this.errorEl.hidden = true;
                this.errorLine = null;
                this.lastSuccess = null;
                this.viewState.baseWidth = 0;
                this.viewState.baseHeight = 0;
                if (this.jumpLineBtn) this.jumpLineBtn.hidden = true;
                if (this.stage) {
                    this.stage.classList.remove("has-diagram");
                    this.stage.innerHTML = '<span class="cb-admin-placeholder">在左侧输入 Mermaid 语法即可实时预览...</span>';
                }
                this._applyTransform();
                this._updateStatsAndGutter();
                this._syncExportButtons();
                this._setStatusPill("就绪", "");
                return;
            }
            if (!window.CBMermaidRenderer) {
                this.errorEl.hidden = false;
                this.errorEl.textContent = "Mermaid 公共渲染引擎未加载";
                this._setStatusPill("引擎未就绪", "error");
                this._syncExportButtons();
                return;
            }

            this._setStatusPill("渲染中…", "rendering");
            window.CBMermaidRenderer.render(rawCode, theme).then(function (res) {
                if (self.destroyed || reqId !== self.latestReqId || !self.root.isConnected) return;
                self.errorEl.hidden = true;
                self.errorLine = null;
                if (self.jumpLineBtn) self.jumpLineBtn.hidden = true;
                self._updateThemeVisuals(res.requestedTheme || theme, res.theme);
                self.lastSuccess = {
                    code: rawCode,
                    requestedTheme: res.requestedTheme || theme,
                    theme: res.theme,
                    svgNode: res.svgNode
                };
                if (self.stage) {
                    self.stage.classList.add("has-diagram");
                    self.stage.innerHTML = "";
                    self.stage.appendChild(res.svgNode);
                    var sz = self._measureSvgBaseSize(res.svgNode);
                    self.viewState.baseWidth = sz.width;
                    self.viewState.baseHeight = sz.height;
                    res.svgNode.style.width = sz.width + "px";
                    res.svgNode.style.height = sz.height + "px";
                    if (self.viewState.autoFit) {
                        self._fitToViewport(true);
                    } else {
                        self._applyTransform();
                    }
                }
                self._updateStatsAndGutter();
                self._syncExportButtons();
                self._setStatusPill("已更新", "ok");
            }).catch(function (err) {
                if (self.destroyed || reqId !== self.latestReqId || !self.root.isConnected) return;
                var line = err && err.cbLine ? err.cbLine : null;
                self.errorLine = line;
                self.errorEl.hidden = false;
                var prefix = self.lastSuccess ? "语法提示（预览仍是上次成功结果）" : "语法解析提示";
                self.errorEl.textContent = prefix + (line ? (" · 第 " + line + " 行: ") : ": ") + (err && err.cbSummary ? err.cbSummary : (err.message || "请检查语法"));
                if (self.jumpLineBtn) {
                    if (line) {
                        self.jumpLineBtn.hidden = false;
                        self.jumpLineBtn.textContent = "定位到第 " + line + " 行";
                    } else {
                        self.jumpLineBtn.hidden = true;
                    }
                }
                self._updateStatsAndGutter();
                self._syncExportButtons();
                self._setStatusPill(self.lastSuccess ? "预览仍是上次成功结果" : "语法有误", "error");
            });
        }

        getValue() { return this.input.value; }
        getState() { return this.input.value; }
        setState(val) {
            this.latestReqId++;
            this.input.value = val == null ? "" : String(val);
            this.viewState.autoFit = true;
            this._updateStatsAndGutter();
            this._syncExportButtons();
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
            var existing = widgetByRoot.get(root);
            if (existing && !existing.destroyed) return existing;
            return new BoundMermaidSplitWidget(root, name);
        }
    }

    if (window.telepath && typeof window.telepath.register === "function") {
        window.telepath.register("content_blocks.widgets.MermaidSplitWidget", MermaidSplitWidgetAdapter);
    }
})(window, document);
