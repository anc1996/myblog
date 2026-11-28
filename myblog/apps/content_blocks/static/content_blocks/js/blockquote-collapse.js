(function () {
    "use strict";

    var BLOCKQUOTE_SELECTOR = [
        ".detail-body-section blockquote",
        ".blog-body-section blockquote",
        ".guide-main blockquote",
        ".project-body-section blockquote"
    ].join(", ");

    // 方案 A 阈值：普通短引用（<= 180px）直接显示；
    // 长引用（> 180px）与首行以「思路/解析/答案/拓展/补充」等引导的引用默认全部收起，点击后展开。
    var COLLAPSE_HEIGHT_PX = 180;
    var KEYWORD_PREFIX_RE = /^(?:思路|解析|答案|解答|拓展|扩展|延伸|参考|补充|推导|证明|附[:：])/;
    var TITLE_MAX_LEN = 64;

    var CHEVRON_SVG =
        '<svg class="cb-bq-chevron" width="14" height="14" viewBox="0 0 24 24" ' +
        'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
        'stroke-linejoin="round" aria-hidden="true">' +
        '<path d="m6 9 6 6 6-6"/></svg>';

    var enhancedWrappers = [];
    var batchBarEl = null;
    var idCounter = 0;

    function cleanLeadingDecor(line) {
        return String(line || "")
            .replace(/^[\s>*\-•·#【\[（(\u2600-\u27BF\uD83C-\uDBFF\uDC00-\uDFFF]+/, "")
            .trim();
    }

    function extractFirstLine(bq) {
        var text = (bq.innerText || bq.textContent || "").trim();
        if (!text) {
            return { firstLine: "", charCount: 0 };
        }
        var lines = text.split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
        var firstLine = lines.length ? lines[0] : "";
        var compactChars = text.replace(/\s+/g, "").length;
        return { firstLine: firstLine, charCount: compactChars };
    }

    function isKeywordLead(firstLine) {
        var cleaned = cleanLeadingDecor(firstLine);
        return KEYWORD_PREFIX_RE.test(cleaned);
    }

    function buildSummaryTitle(firstLine, keywordHit) {
        var raw = String(firstLine || "").trim().replace(/[:：]\s*$/, "");
        if (!raw) {
            return keywordHit ? "解析 / 拓展引用" : "扩展引用";
        }
        if (raw.length > TITLE_MAX_LEN) {
            return raw.slice(0, TITLE_MAX_LEN) + "…";
        }
        return raw;
    }

    function setWrapperExpanded(wrapper, expanded) {
        if (!wrapper) return;
        wrapper.classList.toggle("is-collapsed", !expanded);
        wrapper.classList.toggle("is-expanded", !!expanded);
        var btn = wrapper.querySelector(".cb-bq-header");
        if (btn) {
            btn.setAttribute("aria-expanded", expanded ? "true" : "false");
            var labelSpan = btn.querySelector(".cb-bq-toggle-label");
            if (labelSpan) {
                labelSpan.textContent = expanded ? "收起引用" : "展开引用";
            }
        }
    }

    function syncBatchBarState() {
        if (!batchBarEl) return;
        var total = enhancedWrappers.length;
        var collapsedCount = 0;
        for (var i = 0; i < total; i++) {
            if (enhancedWrappers[i].classList.contains("is-collapsed")) {
                collapsedCount += 1;
            }
        }
        var countEl = batchBarEl.querySelector(".cb-bq-batch-count");
        var btnEl = batchBarEl.querySelector(".cb-bq-batch-btn");
        if (countEl) {
            countEl.textContent = "本文含 " + total + " 处可折叠扩展引用";
        }
        if (btnEl) {
            var allExpanded = collapsedCount === 0;
            btnEl.setAttribute("data-target-action", allExpanded ? "collapse" : "expand");
            btnEl.textContent = allExpanded ? "全部收起" : "全部展开 (" + collapsedCount + ")";
        }
    }

    function ensureBatchBar() {
        if (enhancedWrappers.length < 2) return;
        if (!batchBarEl) {
            var firstWrapper = enhancedWrappers[0];
            if (!firstWrapper || !firstWrapper.parentNode) return;

            var bar = document.createElement("div");
            bar.className = "cb-bq-batch-bar";
            bar.setAttribute("data-cb-bq-batch-bar", "1");

            var countSpan = document.createElement("span");
            countSpan.className = "cb-bq-batch-count";

            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "cb-bq-batch-btn";
            btn.addEventListener("click", function () {
                var action = btn.getAttribute("data-target-action");
                var expandAll = action !== "collapse";
                for (var i = 0; i < enhancedWrappers.length; i++) {
                    setWrapperExpanded(enhancedWrappers[i], expandAll);
                }
                syncBatchBarState();
            });

            bar.appendChild(countSpan);
            bar.appendChild(btn);
            firstWrapper.parentNode.insertBefore(bar, firstWrapper);
            batchBarEl = bar;
        }
        syncBatchBarState();
    }

    function shouldSkipBlockquote(bq) {
        if (!bq || bq.getAttribute("data-bq-enhanced") === "1") return true;
        if (bq.getAttribute("data-no-collapse") === "1") return true;
        if (bq.closest("blockquote") !== bq) return true;
        if (bq.closest("details")) return true;
        if (bq.closest(".cb-bq-collapsible")) return true;
        if (bq.closest(".detail-author-card")) return true;
        return false;
    }

    function enhanceBlockquote(bq) {
        if (shouldSkipBlockquote(bq)) return;

        var rectHeight = bq.getBoundingClientRect ? bq.getBoundingClientRect().height : 0;
        var height = Math.max(bq.scrollHeight || 0, Math.round(rectHeight || 0));
        if (height <= 0) return;

        var info = extractFirstLine(bq);
        var keywordHit = isKeywordLead(info.firstLine);
        var shouldCollapse = height > COLLAPSE_HEIGHT_PX || keywordHit;
        if (!shouldCollapse) {
            return;
        }

        bq.setAttribute("data-bq-enhanced", "1");
        idCounter += 1;
        var viewportId = "cb-bq-viewport-" + idCounter;

        var wrapper = document.createElement("div");
        wrapper.className = "cb-bq-collapsible is-collapsed";
        wrapper.setAttribute("data-cb-bq-collapsible", "1");
        if (keywordHit) {
            wrapper.setAttribute("data-cb-bq-kind", "analysis");
        }

        var viewport = document.createElement("div");
        viewport.className = "cb-bq-viewport";
        viewport.id = viewportId;

        var headerBtn = document.createElement("button");
        headerBtn.type = "button";
        headerBtn.className = "cb-bq-header";
        headerBtn.setAttribute("aria-expanded", "false");
        headerBtn.setAttribute("aria-controls", viewportId);

        var titleSpan = document.createElement("span");
        titleSpan.className = "cb-bq-title";
        titleSpan.textContent = buildSummaryTitle(info.firstLine, keywordHit);

        var rightGroup = document.createElement("span");
        rightGroup.className = "cb-bq-header-right";

        var meta = document.createElement("span");
        meta.className = "cb-bq-meta";
        meta.textContent = info.charCount > 0 ? ("约 " + info.charCount + " 字") : "";

        var togglePill = document.createElement("span");
        togglePill.className = "cb-bq-toggle-pill";
        togglePill.innerHTML = '<span class="cb-bq-toggle-label">展开引用</span>' + CHEVRON_SVG;

        if (meta.textContent) {
            rightGroup.appendChild(meta);
        }
        rightGroup.appendChild(togglePill);

        headerBtn.appendChild(titleSpan);
        headerBtn.appendChild(rightGroup);

        headerBtn.addEventListener("click", function () {
            var willExpand = wrapper.classList.contains("is-collapsed");
            setWrapperExpanded(wrapper, willExpand);
            syncBatchBarState();
        });

        var parent = bq.parentNode;
        if (!parent) return;
        parent.insertBefore(wrapper, bq);
        viewport.appendChild(bq);
        wrapper.appendChild(headerBtn);
        wrapper.appendChild(viewport);
        enhancedWrappers.push(wrapper);
    }

    function expandForHashTarget() {
        var hash = window.location.hash;
        if (!hash || hash.length < 2) return;
        var targetId = "";
        try {
            targetId = decodeURIComponent(hash.slice(1));
        } catch (_err) {
            targetId = hash.slice(1);
        }
        var target = targetId ? document.getElementById(targetId) : null;
        if (!target) return;
        var collapsible = target.closest(".cb-bq-collapsible.is-collapsed");
        if (collapsible) {
            setWrapperExpanded(collapsible, true);
            syncBatchBarState();
        }
    }

    function scanAndEnhance() {
        var nodes = document.querySelectorAll(BLOCKQUOTE_SELECTOR);
        for (var i = 0; i < nodes.length; i++) {
            enhanceBlockquote(nodes[i]);
        }
        ensureBatchBar();
        expandForHashTarget();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", scanAndEnhance);
    } else {
        scanAndEnhance();
    }
    // 等待公式/图片排版落定后二次轻量复检，确保异步撑高的引用块也能收口
    window.addEventListener("load", scanAndEnhance);
    window.addEventListener("hashchange", expandForHashTarget);
})();
