/**
 * 全站代码一键复制通用模块 (code-copy.js)
 * 统一支持 CustomCodeBlock 与 CustomStepBlock，安全处理 URI 编码与回退机制。
 */

(function () {
    "use strict";

    function decodeSafe(str) {
        if (!str) return "";
        try {
            return decodeURIComponent(str);
        } catch (e) {
            return str;
        }
    }

    function copyText(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        }
        return new Promise(function (resolve, reject) {
            var textarea = document.createElement("textarea");
            textarea.value = text;
            textarea.style.position = "fixed";
            textarea.style.left = "-999999px";
            textarea.style.top = "-999999px";
            document.body.appendChild(textarea);
            textarea.focus();
            textarea.select();
            try {
                var successful = document.execCommand("copy");
                document.body.removeChild(textarea);
                if (successful) {
                    resolve();
                } else {
                    reject(new Error("execCommand copy failed"));
                }
            } catch (err) {
                document.body.removeChild(textarea);
                reject(err);
            }
        });
    }

    function initCodeCopy() {
        document.querySelectorAll(".copy-code-btn").forEach(function (btn) {
            if (btn.hasAttribute("data-copy-bound")) return;
            btn.setAttribute("data-copy-bound", "true");

            btn.addEventListener("click", function (e) {
                e.preventDefault();
                var rawAttr = btn.getAttribute("data-clipboard-text");
                var codeToCopy = "";

                if (rawAttr) {
                    codeToCopy = decodeSafe(rawAttr);
                } else {
                    // 回退读取父容器代码内容
                    var container = btn.closest(".code-block, .step-code-container");
                    if (container) {
                        var codeElem = container.querySelector("code");
                        if (codeElem) codeToCopy = codeElem.textContent;
                    }
                }

                if (!codeToCopy) return;

                var originalText = btn.textContent;
                copyText(codeToCopy).then(function () {
                    btn.textContent = "已复制 ✓";
                    btn.classList.add("is-copied");
                    setTimeout(function () {
                        btn.textContent = originalText;
                        btn.classList.remove("is-copied");
                    }, 2000);
                }).catch(function () {
                    btn.textContent = "复制失败";
                    setTimeout(function () {
                        btn.textContent = originalText;
                    }, 2000);
                });
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initCodeCopy);
    } else {
        initCodeCopy();
    }

    window.initCodeCopy = initCodeCopy;
})();
