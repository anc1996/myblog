(function () {
    "use strict";

    var GRADE_LABELS = {
        excellent: "🌟 优秀",
        good: "👍 良好",
        pass: "👌 及格",
        poor: "👎 差"
    };

    function getCookie(name) {
        try {
            if (!document.cookie) return "";
            var parts = document.cookie.split(";");
            var prefix = name + "=";
            for (var i = 0; i < parts.length; i++) {
                var item = parts[i].trim();
                if (item.indexOf(prefix) === 0) {
                    return decodeURIComponent(item.slice(prefix.length));
                }
            }
        } catch (_e) {}
        return "";
    }

    function getCsrfToken(widget) {
        var cookieToken = getCookie("csrftoken");
        if (cookieToken) return cookieToken;
        var input = widget.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? String(input.value || "") : "";
    }

    function generateRequestId() {
        try {
            if (window.crypto && typeof window.crypto.randomUUID === "function") {
                return "r-" + window.crypto.randomUUID();
            }
        } catch (_e) {}
        var rand = Math.random().toString(36).slice(2, 10);
        return "r-" + Date.now().toString(36) + "-" + rand;
    }

    function initWidget(widget) {
        var ratingUrl = widget.getAttribute("data-rating-url") || "";
        var proofUrl = widget.getAttribute("data-reading-proof-url") || "";
        if (!ratingUrl || !proofUrl) return;

        var buttons = Array.prototype.slice.call(
            widget.querySelectorAll("button[data-rating-grade]")
        );
        if (!buttons.length) return;

        var statusEl = widget.querySelector("[data-rating-status]");
        var gateNoticeEl = widget.querySelector("[data-rating-gate-notice]");
        var optionsGroup = widget.querySelector(".article-rating-options");

        var state = {
            authenticated: widget.getAttribute("data-authenticated") === "true",
            isAuthor: widget.getAttribute("data-is-author") === "true",
            canRate: widget.getAttribute("data-authenticated") === "true" && widget.getAttribute("data-is-author") !== "true",
            ratingEnabled: true,
            grade: widget.getAttribute("data-initial-grade") || null,
            version: parseInt(widget.getAttribute("data-initial-version") || "0", 10) || 0,
            liveRevisionId: parseInt(widget.getAttribute("data-live-revision-id") || "0", 10) || 0,
            visitToken: "",
            readingEligible: false,
            isShortBody: false,
            readingProof: "",
            readingProofExpiresAt: 0,
            busy: false
        };
        if (state.grade === "") state.grade = null;

        function setStatus(text, tone) {
            if (!statusEl) return;
            statusEl.textContent = text || "";
            statusEl.className = "article-rating-status" + (tone ? " is-" + tone : "");
        }

        function updateButtonsUI() {
            for (var i = 0; i < buttons.length; i++) {
                var btn = buttons[i];
                var g = btn.getAttribute("data-rating-grade");
                var selected = state.grade === g;
                btn.setAttribute("aria-pressed", selected ? "true" : "false");
                if (selected) {
                    btn.classList.add("is-selected");
                } else {
                    btn.classList.remove("is-selected");
                }

                var allowed = Boolean(
                    !state.busy &&
                    state.authenticated &&
                    !state.isAuthor &&
                    state.ratingEnabled &&
                    state.canRate
                );
                // 未达阅读门槛时允许取消已选评价，其余档位等待阅读门槛满足后激活
                var btnEnabled = allowed && (state.readingEligible || selected);
                btn.disabled = !btnEnabled;
                btn.setAttribute("aria-disabled", btnEnabled ? "false" : "true");
            }

            if (gateNoticeEl) {
                if (!state.authenticated || state.isAuthor || !state.ratingEnabled) {
                    gateNoticeEl.hidden = true;
                } else if (state.readingEligible) {
                    gateNoticeEl.hidden = false;
                    gateNoticeEl.textContent = "已满足有效阅读条件，可点击下方按钮评价；再次点击已选档位可取消评价。";
                } else {
                    gateNoticeEl.hidden = false;
                    gateNoticeEl.textContent = "阅读门槛：请在本页活跃停留 ≥ 15 秒且阅读正文 ≥ 50%（或滑至正文末尾）后提交评价；再次点击已选档位可取消评价。";
                }
            }
        }

        function applyServerState(data) {
            if (!data || typeof data !== "object") return;
            var prevRev = state.liveRevisionId;
            state.authenticated = Boolean(data.authenticated);
            state.isAuthor = Boolean(data.is_author);
            state.canRate = Boolean(data.can_rate);
            state.ratingEnabled = data.rating_enabled !== false;
            state.grade = data.grade || null;
            state.version = parseInt(data.version, 10) || 0;
            if (data.live_revision_id) {
                state.liveRevisionId = parseInt(data.live_revision_id, 10) || state.liveRevisionId;
            }
            if (prevRev && state.liveRevisionId && prevRev !== state.liveRevisionId) {
                state.readingProof = "";
                state.readingProofExpiresAt = 0;
            }
            updateButtonsUI();
        }

        function fetchRatingState() {
            if (typeof window.fetch !== "function" || !state.authenticated) {
                updateButtonsUI();
                return Promise.resolve(null);
            }
            return window.fetch(ratingUrl, {
                method: "GET",
                credentials: "same-origin",
                headers: { "Accept": "application/json" }
            }).then(function (resp) {
                if (resp.status === 401) {
                    state.authenticated = false;
                    state.canRate = false;
                    updateButtonsUI();
                    return null;
                }
                if (!resp.ok) return null;
                return resp.json().then(function (data) {
                    applyServerState(data);
                    return data;
                }).catch(function () { return null; });
            }).catch(function () {
                updateButtonsUI();
                return null;
            });
        }

        function ensureReadingProof() {
            var now = Date.now();
            if (state.readingProof && state.readingProofExpiresAt > now) {
                return Promise.resolve({ ok: true, token: state.readingProof });
            }
            if (!state.visitToken || !state.readingEligible) {
                return Promise.resolve({
                    ok: false,
                    message: "请继续阅读正文（活跃停留 ≥ 15 秒且阅读 ≥ 50% 或到达文末）后再评价。"
                });
            }

            var headers = { "Content-Type": "application/json", "Accept": "application/json" };
            var csrf = getCsrfToken(widget);
            if (csrf) headers["X-CSRFToken"] = csrf;

            return window.fetch(proofUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: headers,
                body: JSON.stringify({
                    visit_token: state.visitToken,
                    is_short_body: Boolean(state.isShortBody)
                })
            }).then(function (resp) {
                return resp.json().catch(function () { return {}; }).then(function (data) {
                    if (resp.ok && data && data.eligible && data.reading_proof) {
                        state.readingProof = String(data.reading_proof);
                        var ttlSec = parseInt(data.expires_in, 10) || 600;
                        state.readingProofExpiresAt = Date.now() + Math.max(30, ttlSec - 15) * 1000;
                        return { ok: true, token: state.readingProof };
                    }
                    var reason = (data && data.reason) || "";
                    if (reason === "threshold_not_met") {
                        return {
                            ok: false,
                            message: "阅读进度尚未达标，请再浏览几秒后再试。"
                        };
                    }
                    if (reason === "stale_revision") {
                        return {
                            ok: false,
                            message: "文章已发布新版本，请刷新页面后重新评价。"
                        };
                    }
                    if (resp.status === 503 || reason === "analytics_unavailable") {
                        return {
                            ok: false,
                            message: "阅读校验服务暂不可用，请稍后再试。"
                        };
                    }
                    return {
                        ok: false,
                        message: "暂未通过阅读校验，请刷新页面或继续阅读后再试。"
                    };
                });
            }).catch(function () {
                return { ok: false, message: "网络异常，无法获取阅读凭证，请稍后再试。" };
            });
        }

        function submitGrade(btnGrade) {
            if (state.busy) return;
            if (!state.authenticated) {
                setStatus("请先登录后再参与文章评价。", "warn");
                return;
            }
            if (state.isAuthor) {
                setStatus("作者本人不可对自己的文章进行评价。", "warn");
                return;
            }
            if (!state.ratingEnabled || !state.canRate) {
                setStatus("当前页面暂不支持评价。", "warn");
                return;
            }

            var targetGrade = (state.grade === btnGrade) ? null : btnGrade;
            state.busy = true;
            updateButtonsUI();
            setStatus(targetGrade === null ? "正在取消评价…" : "正在提交评价…", "");

            var proofPromise = (targetGrade === null)
                ? Promise.resolve({ ok: true, token: "" })
                : ensureReadingProof();

            proofPromise.then(function (proofRes) {
                if (!proofRes.ok) {
                    setStatus(proofRes.message, "warn");
                    return null;
                }

                var headers = { "Content-Type": "application/json", "Accept": "application/json" };
                var csrf = getCsrfToken(widget);
                if (csrf) headers["X-CSRFToken"] = csrf;

                return window.fetch(ratingUrl, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: headers,
                    body: JSON.stringify({
                        target_grade: targetGrade,
                        expected_revision_id: state.liveRevisionId,
                        expected_version: state.version,
                        request_id: generateRequestId(),
                        reading_proof: proofRes.token
                    })
                }).then(function (resp) {
                    return resp.json().catch(function () { return {}; }).then(function (data) {
                        if (resp.ok && data && data.ok) {
                            state.grade = data.grade || null;
                            if (typeof data.version === "number") {
                                state.version = data.version;
                            }
                            if (data.live_revision_id) {
                                state.liveRevisionId = parseInt(data.live_revision_id, 10) || state.liveRevisionId;
                            }
                            if (state.grade) {
                                setStatus("已记录你的评价：「" + (GRADE_LABELS[state.grade] || state.grade) + "」，再次点击可取消。", "ok");
                            } else {
                                setStatus("已取消你的文章评价。", "ok");
                            }
                            return;
                        }

                        var errCode = (data && data.error) || "";
                        if (resp.status === 409) {
                            if (errCode === "stale_revision") {
                                state.readingProof = "";
                                state.readingProofExpiresAt = 0;
                            }
                            setStatus((data && data.message) || "文章或评价状态已变更，正在为你同步最新状态…", "warn");
                            return fetchRatingState();
                        }
                        if (errCode === "invalid_reading_proof") {
                            state.readingProof = "";
                            state.readingProofExpiresAt = 0;
                        }
                        if (resp.status === 429) {
                            setStatus((data && data.message) || "评价操作过于频繁，请稍后再试。", "error");
                            return;
                        }
                        if (resp.status === 503) {
                            setStatus((data && data.message) || "评价服务暂不可用，请稍后再试。", "error");
                            return;
                        }
                        setStatus((data && data.message) || "提交评价失败，请稍后重试。", "error");
                    });
                });
            }).catch(function () {
                setStatus("网络异常，评价提交未完成，请稍后重试。", "error");
            }).then(function () {
                state.busy = false;
                updateButtonsUI();
            });
        }

        for (var i = 0; i < buttons.length; i++) {
            buttons[i].addEventListener("click", function () {
                var g = this.getAttribute("data-rating-grade");
                if (g) submitGrade(g);
            });
        }

        if (optionsGroup) {
            optionsGroup.addEventListener("keydown", function (ev) {
                if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
                var activeIdx = buttons.indexOf(document.activeElement);
                if (activeIdx < 0) return;
                ev.preventDefault();
                var step = ev.key === "ArrowRight" ? 1 : -1;
                var nextIdx = (activeIdx + step + buttons.length) % buttons.length;
                buttons[nextIdx].focus();
            });
        }

        function handleVisitEvent(detail) {
            if (!detail || typeof detail !== "object") return;
            if (detail.visitToken) {
                state.visitToken = String(detail.visitToken);
            }
            if (typeof detail.isShortBody === "boolean") {
                state.isShortBody = detail.isShortBody;
            }
            if (typeof detail.readingEligible === "boolean") {
                state.readingEligible = detail.readingEligible;
            }
            updateButtonsUI();
        }

        window.addEventListener("myblog:analytics:visit", function (ev) {
            handleVisitEvent(ev && ev.detail);
        });

        if (window.__myblogAnalyticsVisit) {
            handleVisitEvent(window.__myblogAnalyticsVisit);
        }

        updateButtonsUI();
        fetchRatingState();
    }

    function boot() {
        var widgets = document.querySelectorAll("[data-analytics-rating-widget]");
        for (var i = 0; i < widgets.length; i++) {
            initWidget(widgets[i]);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
