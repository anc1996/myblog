(function () {
    "use strict";

    var CONTEXT_ENDPOINT = "/api/v1/analytics/context/";
    var EVENTS_ENDPOINT = "/api/v1/analytics/events/";
    var MAX_ACTIVE_SECONDS = 1800;
    var INTERACTION_IDLE_MS = 30000;
    var SHORT_BODY_MIN_ACTIVE_SECONDS = 10;

    function getMetaDescriptor() {
        try {
            var meta = document.querySelector('meta[name="analytics-descriptor"]');
            if (!meta) return "";
            return (meta.getAttribute("content") || "").trim();
        } catch (_e) {
            return "";
        }
    }

    function getCookie(name) {
        try {
            if (!document.cookie) return "";
            var cookies = document.cookie.split(";");
            var prefix = name + "=";
            for (var i = 0; i < cookies.length; i++) {
                var c = cookies[i].trim();
                if (c.indexOf(prefix) === 0) {
                    return decodeURIComponent(c.slice(prefix.length));
                }
            }
        } catch (_e) {}
        return "";
    }

    function parseReferrer() {
        try {
            if (!document.referrer) {
                return { host: "", path: "" };
            }
            var parsed = new URL(document.referrer);
            return {
                host: parsed.host || "",
                path: parsed.pathname || ""
            };
        } catch (_e) {
            return { host: "", path: "" };
        }
    }

    function postJsonFetch(url, payload, keepalive) {
        try {
            if (typeof window.fetch !== "function") return Promise.resolve(null);
            var headers = { "Content-Type": "application/json" };
            var csrf = getCookie("csrftoken");
            if (csrf) {
                headers["X-CSRFToken"] = csrf;
            }
            var opts = {
                method: "POST",
                credentials: "same-origin",
                headers: headers,
                body: JSON.stringify(payload)
            };
            if (keepalive) {
                opts.keepalive = true;
            }
            return window.fetch(url, opts).catch(function () {
                return null;
            });
        } catch (_e) {
            return Promise.resolve(null);
        }
    }

    function postBeacon(url, payload) {
        try {
            var serialized = JSON.stringify(payload);
            if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
                var blob = new Blob([serialized], { type: "application/json" });
                if (navigator.sendBeacon(url, blob)) {
                    return true;
                }
            }
        } catch (_e) {}
        postJsonFetch(url, payload, true);
        return false;
    }

    function init() {
        var descriptor = getMetaDescriptor();
        if (!descriptor) return;

        var contextStarted = false;

        function bootSession() {
            if (contextStarted) return;
            if (document.visibilityState !== "visible") return;
            contextStarted = true;

            var ref = parseReferrer();
            postJsonFetch(
                CONTEXT_ENDPOINT,
                {
                    descriptor: descriptor,
                    referrer_host: ref.host,
                    referrer_path: ref.path
                },
                false
            ).then(function (resp) {
                if (!resp || !resp.ok) return;
                return resp.json().then(function (data) {
                    if (!data || !data.enabled || !data.visit_token) return;
                    startTracking(data);
                }).catch(function () {});
            }).catch(function () {});
        }

        if (document.visibilityState === "visible") {
            bootSession();
        } else {
            document.addEventListener("visibilitychange", function onVisible() {
                if (document.visibilityState === "visible") {
                    document.removeEventListener("visibilitychange", onVisible);
                    bootSession();
                }
            });
        }
    }

    function startTracking(ctx) {
        var visitToken = String(ctx.visit_token);
        var kind = ctx.kind === "entry" ? "entry" : "detail";
        var heartbeatIntervalMs = Math.max(
            3000,
            parseInt(ctx.heartbeat_interval_ms, 10) || 10000
        );
        var seq = 0;

        function nextSeq() {
            seq += 1;
            return seq;
        }

        if (kind === "detail") {
            startDetailTracking(visitToken, heartbeatIntervalMs, nextSeq);
        } else {
            startEntryTracking(visitToken, nextSeq);
        }
    }

    function startDetailTracking(visitToken, heartbeatIntervalMs, nextSeq) {
        var startEl = document.getElementById("article-start");
        var endEl = document.getElementById("article-end");
        var hasMarkers = Boolean(startEl && endEl);

        var activeSeconds = 0;
        var maxScrollPct = 0;
        var reachedEnd = false;
        var isShortBody = false;
        var endIntersected = false;
        var lastInteractionAt = Date.now();

        var lastSentActive = -1;
        var lastSentScroll = -1;
        var lastSentReachedEnd = false;
        var lastEndFlushAt = 0;
        var lastEndSnapshot = "";
        var lastNotifiedEligible = false;
        var syncingThreshold = false;

        function emitVisitEvent(eligible) {
            var detail = {
                visitToken: visitToken,
                readingEligible: Boolean(eligible),
                isShortBody: Boolean(isShortBody),
                activeSeconds: activeSeconds,
                maxScrollPct: maxScrollPct,
                reachedEnd: Boolean(reachedEnd)
            };
            window.__myblogAnalyticsVisit = detail;
            try {
                window.dispatchEvent(new CustomEvent("myblog:analytics:visit", { detail: detail }));
            } catch (_e) {}
        }

        function checkReadingGate() {
            var eligibleNow = Boolean(hasMarkers && activeSeconds >= 15 && (maxScrollPct >= 50 || reachedEnd));
            if (!eligibleNow || lastNotifiedEligible || syncingThreshold) {
                return;
            }
            if (
                activeSeconds !== lastSentActive ||
                maxScrollPct !== lastSentScroll ||
                reachedEnd !== lastSentReachedEnd
            ) {
                syncingThreshold = true;
                var req = postJsonFetch(EVENTS_ENDPOINT, buildPayload("heartbeat"), false);
                markSentState();
                req.then(function () {
                    syncingThreshold = false;
                    lastNotifiedEligible = true;
                    emitVisitEvent(true);
                }, function () {
                    syncingThreshold = false;
                    lastNotifiedEligible = true;
                    emitVisitEvent(true);
                });
            } else {
                lastNotifiedEligible = true;
                emitVisitEvent(true);
            }
        }

        function detectShortBody() {
            if (!hasMarkers) return false;
            try {
                var vh = window.innerHeight || document.documentElement.clientHeight || 0;
                if (vh <= 0) return false;
                var startRect = startEl.getBoundingClientRect();
                var endRect = endEl.getBoundingClientRect();
                var bodyHeight = Math.max(0, endRect.top - startRect.top);
                return (window.scrollY <= 20 && endRect.top <= vh) || bodyHeight <= vh * 0.85;
            } catch (_e) {
                return false;
            }
        }

        function updateScrollProgress() {
            if (!hasMarkers) return;
            try {
                var startRect = startEl.getBoundingClientRect();
                var endRect = endEl.getBoundingClientRect();
                var scrollY = window.scrollY || window.pageYOffset || 0;
                var startTop = startRect.top + scrollY;
                var endTop = endRect.top + scrollY;
                var vh = window.innerHeight || document.documentElement.clientHeight || 0;
                var viewportBottom = scrollY + vh;

                if (endTop <= startTop) {
                    return;
                }
                var rawPct = ((viewportBottom - startTop) / (endTop - startTop)) * 100;
                var clamped = Math.max(0, Math.min(100, Math.round(rawPct)));
                if (clamped > maxScrollPct) {
                    maxScrollPct = clamped;
                }
                if (!isShortBody && viewportBottom >= endTop) {
                    endIntersected = true;
                    reachedEnd = true;
                }
                checkReadingGate();
            } catch (_e) {}
        }

        function buildPayload(eventName) {
            return {
                visit_token: visitToken,
                seq: nextSeq(),
                event: eventName,
                active_seconds: activeSeconds,
                max_scroll_pct: maxScrollPct,
                reached_end: Boolean(reachedEnd),
                is_short_body: Boolean(isShortBody)
            };
        }

        function markSentState() {
            lastSentActive = activeSeconds;
            lastSentScroll = maxScrollPct;
            lastSentReachedEnd = reachedEnd;
        }

        isShortBody = detectShortBody();
        if (!isShortBody) {
            updateScrollProgress();
        }

        // 发送初始 start 事件
        postJsonFetch(EVENTS_ENDPOINT, buildPayload("start"), false);
        markSentState();
        emitVisitEvent(false);
        checkReadingGate();

        // 监听用户真实交互
        var interactionEvents = ["scroll", "mousemove", "keydown", "touchstart", "click"];
        function onUserActivity() {
            lastInteractionAt = Date.now();
        }
        for (var i = 0; i < interactionEvents.length; i++) {
            window.addEventListener(interactionEvents[i], onUserActivity, { passive: true });
        }
        window.addEventListener("scroll", updateScrollProgress, { passive: true });
        window.addEventListener("resize", updateScrollProgress, { passive: true });

        // 监听正文末端锚点可见性
        if (hasMarkers && typeof window.IntersectionObserver === "function") {
            try {
                var observer = new window.IntersectionObserver(
                    function (entries) {
                        for (var idx = 0; idx < entries.length; idx++) {
                            if (entries[idx].isIntersecting) {
                                endIntersected = true;
                                if (!isShortBody) {
                                    maxScrollPct = 100;
                                    reachedEnd = true;
                                } else if (activeSeconds >= SHORT_BODY_MIN_ACTIVE_SECONDS) {
                                    reachedEnd = true;
                                }
                                checkReadingGate();
                            }
                        }
                    },
                    { threshold: 0.01 }
                );
                observer.observe(endEl);
            } catch (_e) {}
        }

        // 1 秒活跃读秒计时器
        window.setInterval(function () {
            try {
                if (document.visibilityState !== "visible") return;
                var now = Date.now();
                if (now - lastInteractionAt > INTERACTION_IDLE_MS) return;
                if (activeSeconds < MAX_ACTIVE_SECONDS) {
                    activeSeconds += 1;
                }
                if (isShortBody && endIntersected && activeSeconds >= SHORT_BODY_MIN_ACTIVE_SECONDS) {
                    maxScrollPct = 100;
                    reachedEnd = true;
                }
                checkReadingGate();
            } catch (_e) {}
        }, 1000);

        // 周期心跳上报
        window.setInterval(function () {
            try {
                if (document.visibilityState !== "visible") return;
                if (
                    activeSeconds === lastSentActive &&
                    maxScrollPct === lastSentScroll &&
                    reachedEnd === lastSentReachedEnd
                ) {
                    return;
                }
                postJsonFetch(EVENTS_ENDPOINT, buildPayload("heartbeat"), false);
                markSentState();
            } catch (_e) {}
        }, heartbeatIntervalMs);

        function flushEnd() {
            try {
                var now = Date.now();
                var snap = activeSeconds + ":" + maxScrollPct + ":" + (reachedEnd ? 1 : 0);
                if (snap === lastEndSnapshot && now - lastEndFlushAt < 500) {
                    return;
                }
                lastEndSnapshot = snap;
                lastEndFlushAt = now;
                postBeacon(EVENTS_ENDPOINT, buildPayload("end"));
                markSentState();
            } catch (_e) {}
        }

        document.addEventListener("visibilitychange", function () {
            if (document.visibilityState === "hidden") {
                flushEnd();
            } else if (document.visibilityState === "visible") {
                lastInteractionAt = Date.now();
            }
        });
        window.addEventListener("pagehide", flushEnd);
    }

    function startEntryTracking(visitToken, nextSeq) {
        postJsonFetch(
            EVENTS_ENDPOINT,
            {
                visit_token: visitToken,
                seq: nextSeq(),
                event: "start"
            },
            false
        );

        var exposedPageIds = {};
        var pendingExposureIds = [];
        var exposureTimer = null;

        function flushExposures(useBeacon) {
            if (exposureTimer !== null) {
                window.clearTimeout(exposureTimer);
                exposureTimer = null;
            }
            if (!pendingExposureIds.length) return;
            var batch = pendingExposureIds.slice();
            pendingExposureIds = [];
            var payload = {
                visit_token: visitToken,
                seq: nextSeq(),
                event: "card_exposure",
                target_page_ids: batch
            };
            if (useBeacon) {
                postBeacon(EVENTS_ENDPOINT, payload);
            } else {
                postJsonFetch(EVENTS_ENDPOINT, payload, false);
            }
        }

        function scheduleExposure(pageId) {
            if (!pageId || exposedPageIds[pageId]) return;
            exposedPageIds[pageId] = true;
            pendingExposureIds.push(pageId);
            if (exposureTimer === null) {
                exposureTimer = window.setTimeout(function () {
                    flushExposures(false);
                }, 350);
            }
        }

        var cardEls = document.querySelectorAll("[data-analytics-card]");
        if (cardEls.length > 0 && typeof window.IntersectionObserver === "function") {
            try {
                var cardObserver = new window.IntersectionObserver(
                    function (entries) {
                        for (var i = 0; i < entries.length; i++) {
                            if (!entries[i].isIntersecting) continue;
                            var rawId = entries[i].target.getAttribute("data-analytics-card");
                            var pid = parseInt(rawId, 10);
                            if (pid > 0) {
                                scheduleExposure(pid);
                            }
                            cardObserver.unobserve(entries[i].target);
                        }
                    },
                    { threshold: 0.1 }
                );
                for (var j = 0; j < cardEls.length; j++) {
                    cardObserver.observe(cardEls[j]);
                }
            } catch (_e) {}
        }

        function handleCardClick(ev) {
            try {
                var target = ev.target;
                if (!target || typeof target.closest !== "function") return;
                var cardEl = target.closest("[data-analytics-card]");
                if (!cardEl) return;
                var anchor = cardEl.tagName === "A" ? cardEl : target.closest("a");
                if (!anchor) return;
                var rawId = cardEl.getAttribute("data-analytics-card");
                var pid = parseInt(rawId, 10);
                if (!(pid > 0)) return;
                postBeacon(EVENTS_ENDPOINT, {
                    visit_token: visitToken,
                    seq: nextSeq(),
                    event: "entry_click",
                    target_page_id: pid
                });
            } catch (_e) {}
        }

        document.addEventListener("click", handleCardClick, true);
        document.addEventListener("auxclick", handleCardClick, true);

        var endSent = false;
        function flushEntryEnd() {
            flushExposures(true);
            if (endSent) return;
            endSent = true;
            postBeacon(EVENTS_ENDPOINT, {
                visit_token: visitToken,
                seq: nextSeq(),
                event: "end"
            });
        }

        document.addEventListener("visibilitychange", function () {
            if (document.visibilityState === "hidden") {
                flushEntryEnd();
            } else if (document.visibilityState === "visible") {
                endSent = false;
            }
        });
        window.addEventListener("pagehide", flushEntryEnd);
    }

    try {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", init);
        } else {
            init();
        }
    } catch (_e) {}
})();
