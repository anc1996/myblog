/**
 * Wagtail 8.1 全站 Draftail 富文本粘贴/拖拽传图适配器 (admin-richtext-paste.js)
 * 零页面/字段名硬编码：自动适配 CustomRichTextBlock、结构化卡片内嵌 RichTextBlock 及普通 RichTextField。
 * 上传落入作者受控 CustomImage（默认 RESTRICTED），完成后插入原生 IMAGE 实体。
 */
(function (window, document) {
    "use strict";

    var LOG_PREFIX = "[CBRichTextUpload]";
    var IMAGE_ID_PATTERN = /^[1-9][0-9]{0,18}$/;

    function getConfig() {
        return window.CB_EDITOR_CONFIG || {};
    }

    function getCsrfToken(form) {
        var inp = (form && form.querySelector('input[name="csrfmiddlewaretoken"]')) ||
            document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (inp && inp.value) return inp.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function dataUrlToImageFile(dataUrl, index) {
        var match = /^data:(image\/[A-Za-z0-9.+-]+)(;base64)?,([\s\S]+)$/.exec(dataUrl || "");
        if (!match) return null;
        try {
            var bytes;
            if (match[2]) {
                var binary = window.atob(match[3]);
                bytes = new Uint8Array(binary.length);
                for (var offset = 0; offset < binary.length; offset += 1) {
                    bytes[offset] = binary.charCodeAt(offset);
                }
            } else {
                bytes = new TextEncoder().encode(decodeURIComponent(match[3]));
            }
            var ext = match[1].split("/")[1].replace(/[^A-Za-z0-9]/g, "") || "png";
            return new File([bytes], "richtext-paste-" + String(index + 1) + "." + ext, {
                type: match[1],
            });
        } catch (err) {
            console.error(LOG_PREFIX, "invalid data url", err);
            return null;
        }
    }

    function parseClipboardHtml(html) {
        var tpl = document.createElement("template");
        tpl.innerHTML = html || "";
        var dataUrls = [];
        tpl.content.querySelectorAll("img").forEach(function (img) {
            var src = img.getAttribute("src") || "";
            if (/^data:image\//i.test(src)) {
                dataUrls.push(src);
            }
            img.remove();
        });
        tpl.content.querySelectorAll("script, style").forEach(function (n) {
            n.remove();
        });
        return {
            dataUrls: dataUrls,
            hasText: Boolean((tpl.content.textContent || "").trim()),
        };
    }

    function fileSignature(file) {
        return [file.name || "", file.type || "", file.size || 0, file.lastModified || 0].join("|");
    }

    function extractPureImageFiles(dataTransfer, isDrop) {
        if (!dataTransfer) return null;
        var files = [];
        var signatures = new Set();
        var hasNonImageFile = false;

        Array.from(dataTransfer.items || []).forEach(function (item) {
            if (item.kind !== "file") return;
            var file = item.getAsFile();
            if (!file) return;
            if (!/^image\//i.test(file.type || "")) {
                hasNonImageFile = true;
                return;
            }
            var sig = fileSignature(file);
            if (!signatures.has(sig)) {
                signatures.add(sig);
                files.push(file);
            }
        });

        Array.from(dataTransfer.files || []).forEach(function (file) {
            if (!/^image\//i.test(file.type || "")) {
                hasNonImageFile = true;
                return;
            }
            var sig = fileSignature(file);
            if (!signatures.has(sig)) {
                signatures.add(sig);
                files.push(file);
            }
        });

        if (hasNonImageFile) return null;
        if (isDrop) {
            return files.length > 0 ? files : null;
        }

        var html = dataTransfer.getData("text/html") || "";
        var plainText = dataTransfer.getData("text/plain") || "";
        var parsedHtml = html ? parseClipboardHtml(html) : null;

        // 混合图文 HTML 交回 Draftail 原生处理；仅当没有真实图片文件且存在纯文本时才退回纯文本粘贴
        // （Windows 资源管理器复制图片文件时常在 text/plain 附带文件名，不能因此放弃传图）
        if ((parsedHtml && parsedHtml.hasText) || (files.length === 0 && !html && plainText.trim() && !/^data:image\//i.test(plainText.trim()))) {
            return null;
        }

        if (files.length === 0 && parsedHtml) {
            parsedHtml.dataUrls.forEach(function (dataUrl, index) {
                var f = dataUrlToImageFile(dataUrl, index);
                if (f) files.push(f);
            });
        }

        if (files.length === 0 && /^data:image\/[A-Za-z0-9.+-]+(?:;base64)?,/i.test(plainText.trim())) {
            var pf = dataUrlToImageFile(plainText.trim(), 0);
            if (pf) files.push(pf);
        }

        return files.length > 0 ? files : null;
    }

    function findDraftailInput(draftailRoot) {
        var node = draftailRoot;
        while (node && node !== document.body) {
            var candidates = Array.from(node.querySelectorAll("input, textarea")).filter(function (el) {
                return Boolean(
                    el.draftailEditor &&
                    typeof el.draftailEditor.getEditorState === "function" &&
                    typeof el.draftailEditor.onChange === "function"
                );
            });
            if (candidates.length === 1) {
                return candidates[0];
            }
            if (candidates.length > 1) {
                return null;
            }
            node = node.parentElement;
        }
        return null;
    }

    function getEditorContext(eventTarget) {
        if (!eventTarget || !eventTarget.closest) return null;
        var draftailRoot = eventTarget.closest(".Draftail-Editor");
        if (!draftailRoot) return null;
        if (!eventTarget.closest('[contenteditable="true"]') && !eventTarget.closest(".DraftEditor-root")) {
            return null;
        }
        var input = findDraftailInput(draftailRoot);
        if (!input) return null;
        return {
            input: input,
            editor: input.draftailEditor,
            root: draftailRoot,
            field: draftailRoot.closest(".w-field") || draftailRoot.parentElement,
            form: input.form || draftailRoot.closest("form"),
        };
    }

    function setStatus(context, message, isError) {
        var container = context.field || context.root;
        if (!container) return;
        var status = container.querySelector("[data-cb-richtext-upload-status]");
        if (!status) {
            status = document.createElement("div");
            status.setAttribute("data-cb-richtext-upload-status", "true");
            status.className = "w-help-text cb-richtext-upload-status";
            status.setAttribute("role", "status");
            status.setAttribute("aria-live", "polite");
            status.hidden = true;
            container.appendChild(status);
        }
        status.textContent = message || "";
        status.hidden = !message;
        status.classList.toggle("cb-richtext-upload-status--error", Boolean(isError));
    }

    function lockEditor(context) {
        var blocked = [
            "beforeinput",
            "compositionstart",
            "cut",
            "drop",
            "keydown",
            "mousedown",
            "paste",
            "pointerdown",
        ];
        var blocker = function (ev) {
            ev.preventDefault();
            ev.stopImmediatePropagation();
        };
        blocked.forEach(function (evName) {
            context.root.addEventListener(evName, blocker, true);
        });
        context.root.dataset.cbRichTextUploading = "true";
        context.root.setAttribute("aria-busy", "true");
        context.root.classList.add("cb-richtext-uploading");

        return function () {
            blocked.forEach(function (evName) {
                context.root.removeEventListener(evName, blocker, true);
            });
            delete context.root.dataset.cbRichTextUploading;
            context.root.removeAttribute("aria-busy");
            context.root.classList.remove("cb-richtext-uploading");
        };
    }

    async function uploadImage(file, form) {
        var cfg = getConfig();
        var uploadUrl = cfg.richtextImageUploadUrl || "";
        var maxSize = Number(cfg.maxImageSize) > 0 ? Number(cfg.maxImageSize) : 10 * 1024 * 1024;

        if (!uploadUrl) throw new Error("richtext_upload_url_missing");
        if (!/^image\//i.test(file.type || "")) throw new Error("not_an_image");
        if (file.size > maxSize) throw new Error("file_too_large");

        var lastError = null;
        for (var attempt = 0; attempt < 2; attempt += 1) {
            try {
                var fd = new FormData();
                fd.append("file", file, file.name || "richtext-paste.png");
                fd.append("alt", "");
                fd.append("format", "left");
                var resp = await window.fetch(uploadUrl, {
                    method: "POST",
                    body: fd,
                    headers: { "X-CSRFToken": getCsrfToken(form) },
                    credentials: "same-origin",
                });
                var payload = null;
                try {
                    payload = await resp.json();
                } catch (e) {
                    lastError = e;
                }
                if (!resp.ok) {
                    var msg = (payload && payload.error && payload.error.message) || ("HTTP " + resp.status);
                    var err = new Error(msg);
                    err.retryable = resp.status >= 500;
                    throw err;
                }
                if (
                    !payload ||
                    !payload.image ||
                    !IMAGE_ID_PATTERN.test(String(payload.image.id || "")) ||
                    !payload.preview ||
                    !payload.preview.url
                ) {
                    throw new Error("invalid_upload_response");
                }
                return {
                    id: payload.image.id,
                    src: payload.preview.url,
                    alt: payload.image.alt || "",
                    format: payload.image.format || "left",
                };
            } catch (err) {
                lastError = err;
                if (attempt > 0 || err.retryable !== true) throw err;
            }
        }
        throw lastError || new Error("upload_failed");
    }

    function insertImages(context, originalSelection, images) {
        var DraftJS = window.DraftJS;
        var nextState = context.editor.getEditorState();
        if (originalSelection) {
            try {
                nextState = DraftJS.EditorState.forceSelection(nextState, originalSelection);
            } catch (_selErr) {
                // 上传期间用户若折叠/重建了编辑器块，回退到当前选区继续插入
                nextState = context.editor.getEditorState();
            }
        }
        images.forEach(function (img) {
            var contentWithEntity = nextState.getCurrentContent().createEntity("IMAGE", "IMMUTABLE", img);
            var entityKey = contentWithEntity.getLastCreatedEntityKey();
            nextState = DraftJS.EditorState.set(nextState, { currentContent: contentWithEntity });
            nextState = DraftJS.AtomicBlockUtils.insertAtomicBlock(nextState, entityKey, " ");
        });
        context.editor.onChange(nextState);
    }

    async function handleImageFiles(context, files) {
        var coordinator = window.CBEditorUploadCoordinator;
        var originalSelection = context.editor.getEditorState().getSelection();
        var unlockEditor = lockEditor(context);
        if (coordinator) coordinator.update(context.form, files.length);
        setStatus(context, "正在上传 " + files.length + " 张图片到作者受控图库…", false);

        try {
            var results = await Promise.all(
                files.map(function (file) {
                    return uploadImage(file, context.form)
                        .then(function (img) {
                            return { image: img, error: null };
                        })
                        .catch(function (err) {
                            console.error(LOG_PREFIX, "upload failed", err);
                            return { image: null, error: err };
                        });
                })
            );

            var images = results.filter(function (r) { return Boolean(r.image); }).map(function (r) { return r.image; });
            var failedCount = results.length - images.length;

            if (images.length > 0 && context.input.isConnected && context.input.draftailEditor === context.editor) {
                insertImages(context, originalSelection, images);
            }

            if (failedCount > 0) {
                setStatus(context, failedCount + " 张图片上传失败，请重新粘贴或点击图片按钮选择。", true);
            } else {
                setStatus(context, "图片已上传至受控图库（默认受限态）并插入正文。", false);
                setTimeout(function () { setStatus(context, "", false); }, 2500);
            }
        } finally {
            if (coordinator) coordinator.update(context.form, -files.length);
            unlockEditor();
        }
    }

    function onPasteOrDrop(event, isDrop) {
        var context = getEditorContext(event.target);
        if (!context) return;

        if (context.root.dataset.cbRichTextUploading === "true") {
            event.preventDefault();
            event.stopImmediatePropagation();
            return;
        }

        var cfg = getConfig();
        if (
            !cfg.richtextImageUploadUrl ||
            !window.fetch ||
            !window.DraftJS ||
            !window.DraftJS.EditorState ||
            !window.DraftJS.AtomicBlockUtils
        ) {
            return;
        }

        var dt = isDrop ? event.dataTransfer : event.clipboardData;
        var files = extractPureImageFiles(dt, isDrop);
        if (!files) return;

        event.preventDefault();
        event.stopImmediatePropagation();
        handleImageFiles(context, files);
    }

    document.addEventListener("paste", function (event) { onPasteOrDrop(event, false); }, true);
    document.addEventListener("drop", function (event) { onPasteOrDrop(event, true); }, true);
})(window, document);
