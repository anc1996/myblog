/**
 * Wagtail 8.1 后台音视频左右分栏预览控制器 (admin-media-split.js)。
 *
 * 契约：
 * 1. 左侧复用 Wagtail 原生选择器与 title_override，右侧复用 ContentBlocksMediaPlayers；
 * 2. 「源码」视图仅只读展示由表单派生的公共 UUID 配置 JSON，绝不反向改表单；
 * 3. 改标题不重建/中断播放；切换素材销毁旧播放器；快速连选通过 requestId + AbortController 防竞态；
 * 4. 取消选择弹窗不清空当前选中的素材；移除块时立即停止音频/视频防止后台漏音。
 */

(function (window, document) {
    "use strict";

    var controllers = new WeakMap();

    function buildGatewayDisplayJson(kind, assetId, titleOverride) {
        return JSON.stringify(
            {
                type: kind,
                asset_id: assetId || null,
                title_override: titleOverride || ""
            },
            null,
            2
        );
    }

    class MediaSplitController {
        constructor(root) {
            this.root = root;
            this.kind = root.getAttribute("data-cb-admin-media-split") || "audio";
            this.chooserField = root.getAttribute("data-chooser-field") || this.kind;
            this.previewUrl = root.getAttribute("data-preview-url") || "";

            this.chooserWrap = root.querySelector('[data-contentpath="' + this.chooserField + '"]');
            this.titleWrap = root.querySelector('[data-contentpath="title_override"]');
            this.hiddenInput = this.chooserWrap ? this.chooserWrap.querySelector('input[type="hidden"]') : null;
            this.titleInput = this.titleWrap ? this.titleWrap.querySelector('input[type="text"], textarea') : null;

            this.settingsTab = root.querySelector('[data-cb-media-tab="settings"]');
            this.jsonTab = root.querySelector('[data-cb-media-tab="json"]');
            this.settingsPane = root.querySelector('[data-cb-media-panel="settings"]');
            this.jsonPane = root.querySelector('[data-cb-media-panel="json"]');
            this.jsonCodeEl = root.querySelector("[data-cb-media-json]");
            this.copyBtn = root.querySelector("[data-cb-media-copy-json]");

            this.statusEl = root.querySelector("[data-cb-media-status]");
            this.messageEl = root.querySelector("[data-cb-media-message]");
            this.previewHost = root.querySelector("[data-cb-media-preview]");

            this.latestReqId = 0;
            this.abortController = null;
            this.currentMeta = null;
            this.lastLoadedPk = null;
            this.inputObserver = null;
            this.destroyed = false;

            this._bindEvents();
            this._syncReadOnlyJson();
            this._handleAssetSelectionChange(true);
        }

        _setStatus(text, stateClass) {
            if (!this.statusEl) return;
            this.statusEl.textContent = text;
            this.statusEl.className = "cb-mmd-status-pill" + (stateClass ? " " + stateClass : "");
        }

        _showMessage(text, isError) {
            if (!this.messageEl) return;
            if (!text) {
                this.messageEl.hidden = true;
                this.messageEl.textContent = "";
                this.messageEl.classList.remove("is-error");
                return;
            }
            this.messageEl.hidden = false;
            this.messageEl.textContent = text;
            this.messageEl.classList.toggle("is-error", !!isError);
        }

        _syncReadOnlyJson() {
            if (!this.jsonCodeEl) return;
            var assetUuid = this.currentMeta ? this.currentMeta.asset_id : null;
            var titleVal = this.titleInput ? this.titleInput.value : "";
            this.jsonCodeEl.textContent = buildGatewayDisplayJson(this.kind, assetUuid, titleVal);
        }

        _clearPreviewPlayer() {
            if (window.ContentBlocksMediaPlayers && this.previewHost) {
                window.ContentBlocksMediaPlayers.destroy(this.previewHost);
            }
            if (this.previewHost) {
                this.previewHost.innerHTML = "";
            }
        }

        _renderPreviewPlayer(meta) {
            this._clearPreviewPlayer();
            if (!this.previewHost || !meta || !meta.src) return;

            var isAudio = meta.kind === "audio";
            var wrapper = document.createElement("div");
            wrapper.className = isAudio ? "media-audio-player" : "media-video-player";
            wrapper.setAttribute("data-cb-media-kind", meta.kind);
            wrapper.setAttribute("data-asset-id", meta.asset_id);

            var titleEl = document.createElement("div");
            titleEl.className = isAudio ? "audio-title" : "video-title";
            var customTitle = this.titleInput ? this.titleInput.value.trim() : "";
            titleEl.textContent = customTitle || meta.title || (isAudio ? "音频播放" : "视频播放");
            wrapper.appendChild(titleEl);

            var stage = document.createElement("div");
            stage.className = "cb-media-stage";
            var mediaEl = document.createElement(isAudio ? "audio" : "video");
            mediaEl.controls = true;
            mediaEl.preload = "metadata";
            if (!isAudio) {
                mediaEl.setAttribute("playsinline", "");
                if (meta.poster) {
                    mediaEl.setAttribute("poster", meta.poster);
                }
            }
            mediaEl.src = meta.src;
            stage.appendChild(mediaEl);
            wrapper.appendChild(stage);
            this.previewHost.appendChild(wrapper);

            if (window.ContentBlocksMediaPlayers) {
                window.ContentBlocksMediaPlayers.init(this.previewHost);
            }
        }

        _updateTitleLiveOnly() {
            this._syncReadOnlyJson();
            if (!this.previewHost || !this.currentMeta) return;
            var titleEl = this.previewHost.querySelector(
                this.kind === "audio" ? ".audio-title" : ".video-title"
            );
            if (titleEl) {
                var customTitle = this.titleInput ? this.titleInput.value.trim() : "";
                titleEl.textContent =
                    customTitle || this.currentMeta.title || (this.kind === "audio" ? "音频播放" : "视频播放");
            }
        }

        _handleAssetSelectionChange(forceReload) {
            if (this.destroyed || !this.hiddenInput) return;
            var pk = (this.hiddenInput.value || "").trim();

            if (!pk) {
                if (this.abortController) {
                    this.abortController.abort();
                    this.abortController = null;
                }
                this.latestReqId += 1;
                this.lastLoadedPk = null;
                this.currentMeta = null;
                this._clearPreviewPlayer();
                this._syncReadOnlyJson();
                this._setStatus("未选素材", "");
                this._showMessage(
                    "请在左侧选择或上传" + (this.kind === "audio" ? "音频" : "视频") + "以预览播放效果。",
                    false
                );
                return;
            }

            if (!forceReload && pk === this.lastLoadedPk && this.currentMeta) {
                return;
            }
            this.lastLoadedPk = pk;

            if (this.abortController) {
                this.abortController.abort();
            }
            var reqId = ++this.latestReqId;
            var controller = typeof AbortController === "function" ? new AbortController() : null;
            this.abortController = controller;

            this._clearPreviewPlayer();
            this.currentMeta = null;
            this._syncReadOnlyJson();
            this._setStatus("加载中", "");
            this._showMessage("正在读取受控预览信息…", false);

            if (!this.previewUrl) {
                this._setStatus("配置缺失", "is-error");
                this._showMessage("预览接口地址未配置，请刷新页面后重试。", true);
                return;
            }

            var self = this;
            var queryUrl =
                this.previewUrl +
                "?kind=" +
                encodeURIComponent(this.kind) +
                "&id=" +
                encodeURIComponent(pk);

            window
                .fetch(queryUrl, {
                    method: "GET",
                    credentials: "same-origin",
                    headers: { Accept: "application/json" },
                    signal: controller ? controller.signal : undefined
                })
                .then(function (resp) {
                    return resp.json().catch(function () {
                        return { ok: false, error: "预览接口返回无效响应" };
                    }).then(function (payload) {
                        return { status: resp.status, ok: resp.ok, payload: payload };
                    });
                })
                .then(function (res) {
                    if (self.destroyed || reqId !== self.latestReqId) return;
                    if ((self.hiddenInput.value || "").trim() !== pk) return;
                    if (!res.ok || !res.payload || !res.payload.ok) {
                        var msg = (res.payload && res.payload.error) || "无法加载该媒体预览";
                        self.currentMeta = null;
                        self._syncReadOnlyJson();
                        self._setStatus("不可预览", "is-error");
                        self._showMessage(msg, true);
                        return;
                    }
                    self.currentMeta = res.payload;
                    self._syncReadOnlyJson();
                    self._showMessage("", false);
                    self._setStatus("已就绪", "is-ready");
                    self._renderPreviewPlayer(res.payload);
                })
                .catch(function (err) {
                    if (err && err.name === "AbortError") return;
                    if (self.destroyed || reqId !== self.latestReqId) return;
                    self.currentMeta = null;
                    self._syncReadOnlyJson();
                    self._setStatus("加载失败", "is-error");
                    self._showMessage("读取受控预览信息失败，请稍后重试。", true);
                });
        }

        _switchTab(tabName) {
            var isJson = tabName === "json";
            if (this.settingsTab) {
                this.settingsTab.classList.toggle("is-active", !isJson);
                this.settingsTab.setAttribute("aria-selected", String(!isJson));
            }
            if (this.jsonTab) {
                this.jsonTab.classList.toggle("is-active", isJson);
                this.jsonTab.setAttribute("aria-selected", String(isJson));
            }
            if (this.settingsPane) this.settingsPane.hidden = isJson;
            if (this.jsonPane) this.jsonPane.hidden = !isJson;
            if (isJson) this._syncReadOnlyJson();
        }

        _bindEvents() {
            var self = this;
            if (this.settingsTab) {
                this.settingsTab.addEventListener("click", function () {
                    self._switchTab("settings");
                });
            }
            if (this.jsonTab) {
                this.jsonTab.addEventListener("click", function () {
                    self._switchTab("json");
                });
            }
            if (this.copyBtn) {
                this.copyBtn.addEventListener("click", function () {
                    var text = self.jsonCodeEl ? self.jsonCodeEl.textContent || "" : "";
                    if (!text || !navigator.clipboard) return;
                    navigator.clipboard.writeText(text).then(function () {
                        var old = self.copyBtn.textContent;
                        self.copyBtn.textContent = "已复制";
                        window.setTimeout(function () {
                            if (!self.destroyed) self.copyBtn.textContent = old;
                        }, 1500);
                    }).catch(function () {});
                });
            }
            if (this.titleInput) {
                this.titleInput.addEventListener("input", function () {
                    self._updateTitleLiveOnly();
                });
            }
            if (this.hiddenInput) {
                this.hiddenInput.addEventListener("change", function () {
                    self._handleAssetSelectionChange(false);
                });
                if (typeof MutationObserver === "function") {
                    this.inputObserver = new MutationObserver(function () {
                        self._handleAssetSelectionChange(false);
                    });
                    this.inputObserver.observe(this.hiddenInput, {
                        attributes: true,
                        attributeFilter: ["value"]
                    });
                }
            }
        }

        destroy() {
            this.destroyed = true;
            if (this.abortController) {
                this.abortController.abort();
                this.abortController = null;
            }
            if (this.inputObserver) {
                this.inputObserver.disconnect();
                this.inputObserver = null;
            }
            this._clearPreviewPlayer();
        }
    }

    function isReadyToBind(root) {
        var field = root.getAttribute("data-chooser-field") || root.getAttribute("data-cb-admin-media-split");
        var chooserWrap = root.querySelector('[data-contentpath="' + field + '"]');
        var titleWrap = root.querySelector('[data-contentpath="title_override"]');
        return !!(
            chooserWrap &&
            chooserWrap.querySelector('input[type="hidden"]') &&
            titleWrap &&
            titleWrap.querySelector('input[type="text"], textarea')
        );
    }

    function scanAndMount(scope) {
        var container = scope || document;
        container.querySelectorAll("[data-cb-admin-media-split]").forEach(function (root) {
            if (controllers.has(root)) return;
            if (!isReadyToBind(root)) return;
            controllers.set(root, new MediaSplitController(root));
        });
    }

    function cleanupRemoved(node) {
        if (!node || node.nodeType !== 1) return;
        if (node.hasAttribute && node.hasAttribute("data-cb-admin-media-split")) {
            var ctrl = controllers.get(node);
            if (ctrl) {
                ctrl.destroy();
                controllers.delete(node);
            }
        }
        if (node.querySelectorAll) {
            node.querySelectorAll("[data-cb-admin-media-split]").forEach(function (child) {
                var ctrl = controllers.get(child);
                if (ctrl) {
                    ctrl.destroy();
                    controllers.delete(child);
                }
            });
        }
    }

    function startObserver() {
        scanAndMount(document);
        if (typeof MutationObserver !== "function" || !document.body) return;
        var observer = new MutationObserver(function (mutations) {
            var shouldScan = false;
            for (var i = 0; i < mutations.length; i++) {
                var m = mutations[i];
                if (m.removedNodes && m.removedNodes.length) {
                    m.removedNodes.forEach(cleanupRemoved);
                }
                if (m.addedNodes && m.addedNodes.length) {
                    shouldScan = true;
                }
            }
            if (shouldScan) {
                scanAndMount(document);
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", startObserver);
    } else {
        startObserver();
    }
})(window, document);
