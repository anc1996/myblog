/**
 * 复杂表格左右分栏（左侧 JSON 源码 / 右侧渲染表格）与复制交互 (table-block.js)
 */

(function (window, document) {
    "use strict";

    function copyFallback(text, onOk) {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        try {
            if (document.execCommand("copy") && onOk) onOk();
        } finally {
            ta.remove();
        }
    }

    function initTableBlocks(root) {
        var container = root || document;
        container.querySelectorAll("[data-cb-table]").forEach(function (block) {
            if (block.hasAttribute("data-cb-table-bound")) return;
            block.setAttribute("data-cb-table-bound", "true");

            var splitBtn = block.querySelector(".cb-table-split-btn");
            var copyBtn = block.querySelector(".cb-table-copy-btn");
            var sourcePane = block.querySelector(".cb-table-source-pane");
            var sourceCode = block.querySelector("code[data-cb-table-source]");

            if (splitBtn && sourcePane) {
                splitBtn.addEventListener("click", function () {
                    var isSplit = block.classList.toggle("is-split-view");
                    splitBtn.setAttribute("aria-pressed", String(isSplit));
                    sourcePane.hidden = !isSplit;
                    if (isSplit && typeof window.enhanceCodeBlocks === "function") {
                        window.enhanceCodeBlocks(sourcePane);
                    }
                });
            }

            if (copyBtn && sourceCode) {
                copyBtn.addEventListener("click", function () {
                    var raw = sourceCode.textContent || "";
                    var onCopied = function () {
                        var old = copyBtn.textContent;
                        copyBtn.textContent = "已复制 ✓";
                        setTimeout(function () { copyBtn.textContent = old; }, 2000);
                    };
                    if (navigator.clipboard && window.isSecureContext) {
                        navigator.clipboard.writeText(raw).then(onCopied).catch(function () {
                            copyFallback(raw, onCopied);
                        });
                    } else {
                        copyFallback(raw, onCopied);
                    }
                });
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            initTableBlocks(document);
        });
    } else {
        initTableBlocks(document);
    }

    window.initTableBlocks = initTableBlocks;
})(window, document);
