/**
 * 全站后台编辑器统一上传并发协调器 (admin-upload-coordinator.js)
 * 供 Vditor (Markdown) 与 Draftail (RichText) 共用，确保同一页面内存在未完成图片/附件上传时，
 * 阻止提前保存草稿、送审或发布，上传全部结束后自动解锁表单提交按钮。
 */
(function (window) {
    "use strict";

    var LOG_PREFIX = "[CBEditorUploadCoordinator]";

    function createCoordinator() {
        var states = new WeakMap();

        function restoreControls(state) {
            state.controls.forEach(function (saved) {
                if (!saved.element || !saved.element.isConnected) return;
                // 仅恢复本协调器加锁过的控件；若外部脚本另行置 disabled 且清除了锁标记则不覆盖
                if (saved.element.getAttribute("data-cb-upload-locked") !== "true") return;
                saved.element.removeAttribute("data-cb-upload-locked");
                saved.element.disabled = saved.disabled;
                if (saved.ariaBusy == null) {
                    saved.element.removeAttribute("aria-busy");
                } else {
                    saved.element.setAttribute("aria-busy", saved.ariaBusy);
                }
            });
            state.controls = [];
        }

        function ensureBanner(form) {
            if (!form) return null;
            var banner = form.querySelector("[data-cb-upload-guard-banner]");
            if (!banner) {
                banner = document.createElement("div");
                banner.setAttribute("data-cb-upload-guard-banner", "true");
                banner.className = "cb-editor-upload-guard-banner";
                banner.setAttribute("role", "status");
                banner.setAttribute("aria-live", "polite");
                banner.hidden = true;
                form.prepend(banner);
            }
            return banner;
        }

        function updateBanner(form, count) {
            var banner = ensureBanner(form);
            if (!banner) return;
            if (count > 0) {
                banner.textContent = "当前有 " + count + " 项媒体仍在上传中，上传完成后即可保存或发布。";
                banner.hidden = false;
            } else {
                banner.textContent = "";
                banner.hidden = true;
            }
        }

        return {
            update: function (form, delta) {
                if (!form || !delta) return;
                var state = states.get(form);
                if (!state) {
                    state = {
                        count: 0,
                        controls: [],
                        handleSubmit: function (event) {
                            if (state.count < 1) return;
                            event.preventDefault();
                            event.stopImmediatePropagation();
                            console.warn(LOG_PREFIX, "submit blocked while uploads pending", {
                                pendingUploads: state.count,
                            });
                            updateBanner(form, state.count);
                        },
                    };
                    states.set(form, state);
                }

                var prev = state.count;
                state.count = Math.max(0, state.count + delta);
                updateBanner(form, state.count);

                if (prev === 0 && state.count > 0) {
                    state.controls = Array.from(
                        form.querySelectorAll('button[type="submit"], input[type="submit"]')
                    ).map(function (control) {
                        var saved = {
                            element: control,
                            disabled: control.disabled,
                            ariaBusy: control.getAttribute("aria-busy"),
                        };
                        control.disabled = true;
                        control.setAttribute("aria-busy", "true");
                        control.setAttribute("data-cb-upload-locked", "true");
                        return saved;
                    });
                    form.addEventListener("submit", state.handleSubmit, true);
                } else if (prev > 0 && state.count === 0) {
                    restoreControls(state);
                    form.removeEventListener("submit", state.handleSubmit, true);
                }
            },
            pending: function (form) {
                var state = form ? states.get(form) : null;
                return state ? state.count : 0;
            },
        };
    }

    if (!window.CBEditorUploadCoordinator || typeof window.CBEditorUploadCoordinator.update !== "function") {
        window.CBEditorUploadCoordinator = createCoordinator();
    }
})(window);
