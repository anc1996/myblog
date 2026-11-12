(function () {
    "use strict";

    function getCsrfToken(form) {
        var input = form ? form.querySelector('input[name="csrfmiddlewaretoken"]') : null;
        if (input && input.value) {
            return input.value;
        }
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function initCaptchaWidget(root) {
        if (!root || root.dataset.captchaBound === "1") {
            return;
        }
        root.dataset.captchaBound = "1";

        var issueUrl = root.getAttribute("data-issue-url") || "/api/v1/captcha/challenges/";
        var imgEl = root.querySelector(".js-captcha-image");
        var idInput = root.querySelector('input[name="captcha_id"]');
        var codeInput = root.querySelector('input[name="captcha_code"]');
        var refreshBtn = root.querySelector(".js-captcha-refresh");
        var statusEl = root.querySelector(".js-captcha-status");
        var form = root.closest("form");
        var latestSeq = 0;
        var inFlight = false;

        async function refreshChallenge() {
            var scope = root.getAttribute("data-captcha-scope") || "";
            if (!scope || !issueUrl || !idInput || !imgEl || inFlight) {
                return;
            }
            inFlight = true;
            var seq = ++latestSeq;
            var prevId = (idInput.value || "").trim();
            if (refreshBtn) {
                refreshBtn.disabled = true;
            }
            if (statusEl) {
                statusEl.textContent = "正在刷新验证码...";
            }
            try {
                var resp = await fetch(issueUrl, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": getCsrfToken(form),
                        "X-Requested-With": "XMLHttpRequest"
                    },
                    body: JSON.stringify({
                        scope: scope,
                        previous_captcha_id: prevId
                    })
                });
                var data = await resp.json().catch(function () { return {}; });
                if (seq !== latestSeq) {
                    return; // 丢弃迟到的旧响应
                }
                if (!resp.ok || data.status !== "success") {
                    if (statusEl) {
                        statusEl.textContent = data.message || "验证码获取失败，请稍后重试。";
                    }
                    return;
                }
                idInput.value = data.captcha_id || "";
                imgEl.src = data.image_data_url || data.image_url || "";
                imgEl.hidden = false;
                if (codeInput) {
                    codeInput.value = "";
                }
                if (statusEl) {
                    statusEl.textContent = "点击图片或“换一张”可刷新，区分度已优化";
                }
            } catch (err) {
                if (seq === latestSeq && statusEl) {
                    statusEl.textContent = "网络异常，暂时无法刷新验证码。";
                }
            } finally {
                inFlight = false;
                if (refreshBtn && seq === latestSeq) {
                    refreshBtn.disabled = false;
                }
            }
        }

        if (refreshBtn) {
            refreshBtn.addEventListener("click", refreshChallenge);
        }
        if (imgEl) {
            imgEl.addEventListener("click", refreshChallenge);
        }
        root._refreshCaptchaChallenge = refreshChallenge;

        var deferLoad = root.getAttribute("data-defer-load") === "true";
        if (!deferLoad && idInput && !idInput.value && imgEl && !imgEl.getAttribute("src")) {
            refreshChallenge();
        }
    }

    window.MyblogCaptcha = {
        initWidget: initCaptchaWidget,
        refreshWidget: function (root) {
            if (!root) {
                return;
            }
            initCaptchaWidget(root);
            if (typeof root._refreshCaptchaChallenge === "function") {
                root._refreshCaptchaChallenge();
            }
        }
    };

    document.addEventListener("DOMContentLoaded", function () {
        var widgets = document.querySelectorAll(".js-captcha-widget");
        widgets.forEach(initCaptchaWidget);
    });
})();
