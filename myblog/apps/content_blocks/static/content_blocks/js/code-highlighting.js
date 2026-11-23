/**
 * 全站公共代码块语法高亮、行号、超长折叠与统一外壳引擎 (code-highlighting.js)
 * 1. 按需懒加载：无代码块零请求；先载入 highlight.min.js，再按需加载页面实际用到的语言包；
 * 2. 统一覆盖：CustomMarkdownBlock 代码围栏、CustomCodeBlock 与 CustomStepBlock；
 * 3. 自动跳过 Mermaid 源码展示区 (.cb-mermaid-source)；
 * 4. 与 code-copy.js 协同：统一补全 .copy-code-btn 并触发 window.initCodeCopy()。
 */

(function (window, document) {
    "use strict";

    var CORE_URL = "/static/vendor/highlightjs/highlight.min.js";
    var LANGUAGE_BASE_URL = "/static/vendor/highlightjs/languages/";
    var COLLAPSE_AFTER_LINES = 14;

    var CODE_SELECTOR = [
        ".detail-body-section pre > code",
        ".blog-body-section pre > code",
        ".blog-intro-card pre > code",
        ".guide-main pre > code",
        ".project-body-section pre > code",
        ".code-block pre > code",
        ".step-code-container pre > code",
        ".cb-cell-code pre > code",
        ".cb-table-source-pane pre > code",
        "[data-code-highlight] pre > code"
    ].join(", ");

    var LANGUAGE_ALIASES = {
        js: "javascript",
        py: "python",
        html: "xml",
        htm: "xml",
        sh: "bash",
        shell: "bash",
        zsh: "bash",
        ps1: "powershell",
        yml: "yaml",
        cmd: "dos",
        text: "plaintext",
        txt: "plaintext",
        md: "markdown",
        csv: "plaintext",
        "c#": "csharp",
        "c++": "cpp",
        postgresql: "pgsql",
        postgres: "pgsql"
    };

    var LANGUAGE_LABELS = {
        bash: "Shell",
        cpp: "C++",
        csharp: "C#",
        css: "CSS",
        dockerfile: "Dockerfile",
        dos: "Command Prompt",
        go: "Go",
        java: "Java",
        javascript: "JavaScript",
        json: "JSON",
        makefile: "Makefile",
        markdown: "Markdown",
        nginx: "Nginx",
        pgsql: "PostgreSQL",
        plaintext: "Text",
        powershell: "PowerShell",
        python: "Python",
        rust: "Rust",
        sql: "SQL",
        typescript: "TypeScript",
        xml: "HTML / XML",
        yaml: "YAML"
    };

    var coreLoadPromise = null;
    var languageLoadPromises = new Map();

    function loadHljsCore() {
        if (window.hljs) return Promise.resolve(window.hljs);
        if (coreLoadPromise) return coreLoadPromise;

        coreLoadPromise = new Promise(function (resolve, reject) {
            var script = document.createElement("script");
            script.src = CORE_URL;
            script.async = true;
            script.onload = function () {
                if (window.hljs) {
                    window.hljs.configure({ ignoreUnescapedHTML: true });
                    resolve(window.hljs);
                } else {
                    reject(new Error("Highlight.js global missing"));
                }
            };
            script.onerror = function () {
                reject(new Error("Highlight.js core failed to load"));
            };
            document.head.appendChild(script);
        });
        return coreLoadPromise;
    }

    function loadLanguage(language) {
        if (!language || language === "plaintext") return Promise.resolve();
        if (window.hljs && window.hljs.getLanguage(language)) return Promise.resolve();
        if (languageLoadPromises.has(language)) return languageLoadPromises.get(language);

        var promise = new Promise(function (resolve) {
            var script = document.createElement("script");
            script.src = LANGUAGE_BASE_URL + encodeURIComponent(language) + ".min.js";
            script.async = true;
            script.onload = resolve;
            script.onerror = function () {
                resolve();
            };
            document.head.appendChild(script);
        });
        languageLoadPromises.set(language, promise);
        return promise;
    }

    function detectLanguage(code) {
        var container = code.closest("[data-language]");
        var classText = (code.className || "") + " " + (code.parentElement ? code.parentElement.className : "");
        var match = classText.match(/(?:lang(?:uage)?)-([a-zA-Z0-9#+._-]+)/i);
        var raw = match ? match[1].toLowerCase() : (container ? (container.getAttribute("data-language") || "").toLowerCase() : "");
        if (!raw) return "";
        return LANGUAGE_ALIASES[raw] || raw;
    }

    function formatLanguageLabel(lang) {
        if (!lang) return "Code";
        return LANGUAGE_LABELS[lang] || lang.toUpperCase();
    }

    function addLineNumbers(pre, lineCount) {
        if (pre.querySelector(".hljs-line-numbers")) return;
        var gutter = document.createElement("div");
        gutter.className = "hljs-line-numbers";
        gutter.setAttribute("aria-hidden", "true");
        var nums = [];
        for (var i = 1; i <= lineCount; i++) {
            nums.push(String(i));
        }
        gutter.textContent = nums.join("\n");
        pre.prepend(gutter);
    }

    function ensureWrapperShell(pre, rawText, lang, lineCount) {
        var existingShell = pre.closest(".code-block, .step-code-container, .code-block-shell, .cb-table-source-pane");
        if (existingShell) {
            var metaTarget = existingShell.querySelector(".code-header");
            if (metaTarget && !metaTarget.querySelector(".code-line-count")) {
                var countSpan = document.createElement("span");
                countSpan.className = "code-line-count";
                countSpan.textContent = lineCount + " 行";
                var titleEl = metaTarget.querySelector(".code-title, .code-lang-label");
                if (titleEl) {
                    titleEl.insertAdjacentElement("afterend", countSpan);
                }
            }
            return existingShell;
        }

        // Markdown 围栏原生 <pre><code>：包裹成统一 .code-block-shell
        var shell = document.createElement("div");
        shell.className = "code-block-shell";
        shell.setAttribute("data-language", lang || "plaintext");

        var toolbar = document.createElement("div");
        toolbar.className = "code-block-toolbar";

        var meta = document.createElement("div");
        meta.className = "code-block-meta";

        var langSpan = document.createElement("span");
        langSpan.className = "code-language";
        langSpan.textContent = formatLanguageLabel(lang);

        var countSpan = document.createElement("span");
        countSpan.className = "code-line-count";
        countSpan.textContent = lineCount + " 行";
        meta.append(langSpan, countSpan);

        var actions = document.createElement("div");
        actions.className = "code-block-actions";

        var copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.className = "copy-code-btn";
        copyBtn.setAttribute("data-clipboard-text", encodeURIComponent(rawText));
        copyBtn.textContent = "复制";
        actions.appendChild(copyBtn);

        toolbar.append(meta, actions);

        var body = document.createElement("div");
        body.className = "code-body";

        pre.parentNode.insertBefore(shell, pre);
        body.appendChild(pre);
        shell.append(toolbar, body);
        return shell;
    }

    function attachCollapseIfLong(shell, lineCount) {
        if (lineCount <= COLLAPSE_AFTER_LINES) return;
        if (shell.classList.contains("code-block-runner")) return;
        if (shell.querySelector(".code-toggle-btn")) return;

        shell.classList.add("is-code-collapsed");
        var footer = document.createElement("div");
        footer.className = "code-expand-footer";

        var toggleBtn = document.createElement("button");
        toggleBtn.type = "button";
        toggleBtn.className = "code-toggle-btn";
        toggleBtn.setAttribute("aria-expanded", "false");
        toggleBtn.textContent = "展开全部 " + lineCount + " 行 ▼";

        toggleBtn.addEventListener("click", function () {
            var collapsed = shell.classList.toggle("is-code-collapsed");
            toggleBtn.setAttribute("aria-expanded", String(!collapsed));
            toggleBtn.textContent = collapsed ? ("展开全部 " + lineCount + " 行 ▼") : "收起代码 ▲";
        });

        footer.appendChild(toggleBtn);
        var body = shell.querySelector(".code-body");
        if (body) {
            body.insertAdjacentElement("afterend", footer);
        } else {
            shell.appendChild(footer);
        }
    }

    function enhanceCodeBlocks(root) {
        var container = root || document;
        var codeNodes = Array.from(container.querySelectorAll(CODE_SELECTOR)).filter(function (code) {
            if (code.hasAttribute("data-cb-code-enhanced")) return false;
            if (code.closest("[data-cb-mermaid]") || code.closest(".cb-mermaid")) return false;
            return true;
        });

        if (codeNodes.length === 0) return;

        loadHljsCore().then(function (hljs) {
            var langs = Array.from(new Set(codeNodes.map(detectLanguage).filter(Boolean)));
            return Promise.all(langs.map(loadLanguage)).then(function () {
                codeNodes.forEach(function (code) {
                    code.setAttribute("data-cb-code-enhanced", "true");
                    var pre = code.parentElement;
                    if (!pre || pre.tagName !== "PRE") return;

                    var rawText = code.textContent.replace(/\r\n/g, "\n").replace(/\n$/, "");
                    var lines = rawText ? rawText.split("\n") : [""];
                    var lineCount = lines.length;
                    var lang = detectLanguage(code);

                    if (lang && hljs.getLanguage(lang)) {
                        code.innerHTML = hljs.highlight(rawText, { language: lang, ignoreIllegals: true }).value;
                        code.classList.add("hljs");
                    } else if (rawText) {
                        code.innerHTML = hljs.highlightAuto(rawText).value;
                        code.classList.add("hljs");
                    }

                    pre.classList.add("code-highlight-pre");
                    var shell = ensureWrapperShell(pre, rawText, lang, lineCount);
                    addLineNumbers(pre, lineCount);
                    attachCollapseIfLong(shell, lineCount);
                });

                if (typeof window.initCodeCopy === "function") {
                    window.initCodeCopy();
                }
            });
        }).catch(function (err) {
            console.warn("Code highlighting fallback:", err);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            enhanceCodeBlocks(document);
        });
    } else {
        enhanceCodeBlocks(document);
    }

    window.enhanceCodeBlocks = enhanceCodeBlocks;
})(window, document);
