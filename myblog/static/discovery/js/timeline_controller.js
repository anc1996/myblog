/**
 * 月历归档与时间线分页控制器 (TimelineController)
 * 保留原生 <a href> SSR 降级能力；JS 可用时通过 format=partial 无刷切换日历与分页。
 */
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var container = document.getElementById("timeline-dynamic-container");
    if (!container) return;

    var activeAbort = null;

    function fetchTimeline(url, pushState) {
      if (activeAbort) activeAbort.abort();
      activeAbort = new AbortController();

      container.style.opacity = "0.55";
      container.style.pointerEvents = "none";

      fetch(url, {
        headers: {
          "X-Listing-Response": "fragment-v1",
          "X-Requested-With": "XMLHttpRequest",
        },
        signal: activeAbort.signal,
      })
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (res) {
          container.style.opacity = "1";
          container.style.pointerEvents = "auto";
          if (res.data && res.data.html) {
            container.innerHTML = res.data.html;
            if (window.MyblogUI && typeof window.MyblogUI.mount === "function") {
              window.MyblogUI.mount(container);
            }
          }
          if (pushState) {
            var targetUrl = new URL(url, window.location.origin);
            window.history.pushState({ path: targetUrl.href }, "", targetUrl.href);
          }
        })
        .catch(function (err) {
          if (err.name === "AbortError") return;
          container.style.opacity = "1";
          container.style.pointerEvents = "auto";
        });
    }

    function buildUrlWithOverrides(overrides) {
      var url = new URL(window.location.href);
      Object.keys(overrides).forEach(function (k) {
        if (overrides[k] === null || overrides[k] === "") {
          url.searchParams.delete(k);
        } else {
          url.searchParams.set(k, overrides[k]);
        }
      });
      return url.pathname + url.search;
    }

    container.addEventListener("click", function (e) {
      var navLink = e.target.closest(".timeline-nav-link");
      var nextBtn = e.target.closest(".listing-page-next-btn");
      var prevBtn = e.target.closest(".listing-page-prev-btn");
      var pageNumBtn = e.target.closest(".listing-page-num-btn");
      var oldestBtn = e.target.closest(".listing-oldest-btn");

      if (navLink) {
        e.preventDefault();
        var href = navLink.getAttribute("href");
        if (href) fetchTimeline(href, true);
      } else if (nextBtn) {
        e.preventDefault();
        var c1 = nextBtn.getAttribute("data-cursor");
        if (c1) fetchTimeline(buildUrlWithOverrides({ cursor: c1, page: null, seek_date: null, seek_direction: null }), true);
      } else if (prevBtn) {
        e.preventDefault();
        var c2 = prevBtn.getAttribute("data-cursor");
        if (c2) fetchTimeline(buildUrlWithOverrides({ cursor: c2, page: null, seek_date: null, seek_direction: null }), true);
      } else if (pageNumBtn) {
        e.preventDefault();
        var p = pageNumBtn.getAttribute("data-page");
        if (p) fetchTimeline(buildUrlWithOverrides({ page: p, cursor: null, seek_date: null, seek_direction: null }), true);
      } else if (oldestBtn) {
        e.preventDefault();
        fetchTimeline(buildUrlWithOverrides({ seek_direction: "oldest", cursor: null, page: null, seek_date: null }), true);
      }
    });

    container.addEventListener("submit", function (e) {
      var ymForm = e.target.closest(".timeline-ym-jump-form");
      var dayForm = e.target.closest(".timeline-day-jump-form");
      var scopeForm = e.target.closest(".timeline-scope-filter-form");
      var jumpForm = e.target.closest(".listing-jump-page-form");
      var seekForm = e.target.closest(".listing-seek-date-form");
      if (ymForm) {
        e.preventDefault();
        var ySel = ymForm.querySelector('[name="year"]');
        var mSel = ymForm.querySelector('[name="month"]');
        var yVal = ySel ? ySel.value.trim() : "";
        var mVal = mSel ? mSel.value.trim() : "";
        if (yVal) {
          fetchTimeline(
            buildUrlWithOverrides({
              year: yVal,
              month: mVal || "1",
              day: null,
              scope: null,
              cursor: null,
              page: null,
              seek_date: null,
              seek_direction: null,
            }),
            true
          );
        }
      } else if (dayForm) {
        e.preventDefault();
        var dayInput = dayForm.querySelector('[name="day"]');
        var dayVal = dayInput ? dayInput.value.trim() : "";
        if (dayVal) {
          var parts = dayVal.split("-");
          fetchTimeline(
            buildUrlWithOverrides({
              year: parts[0] || null,
              month: parts[1] ? String(parseInt(parts[1], 10)) : null,
              day: dayVal,
              scope: null,
              cursor: null,
              page: null,
              seek_date: null,
              seek_direction: null,
            }),
            true
          );
        }
      } else if (scopeForm) {
        e.preventDefault();
        var qInput = scopeForm.querySelector('[name="q"]');
        var sInput = scopeForm.querySelector('[name="sort"]');
        fetchTimeline(
          buildUrlWithOverrides({
            q: qInput ? qInput.value.trim() : null,
            sort: sInput ? sInput.value : null,
            cursor: null,
            page: null,
            seek_date: null,
            seek_direction: null,
          }),
          true
        );
      } else if (jumpForm) {
        e.preventDefault();
        var pInput = jumpForm.querySelector('input[name="page"]');
        var pVal = pInput ? pInput.value.trim() : "";
        if (pVal) fetchTimeline(buildUrlWithOverrides({ page: pVal, cursor: null, seek_date: null, seek_direction: null }), true);
      } else if (seekForm) {
        e.preventDefault();
        var dInput = seekForm.querySelector('input[name="seek_date"]');
        var dVal = dInput ? dInput.value.trim() : "";
        if (dVal) fetchTimeline(buildUrlWithOverrides({ seek_date: dVal, cursor: null, page: null, seek_direction: null }), true);
      }
    });

    window.addEventListener("popstate", function () {
      fetchTimeline(window.location.href, false);
    });
  });
})();
