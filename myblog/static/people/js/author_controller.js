/**
 * 全站作者专栏大厅与作者个人专栏前端控制器 (AuthorController)
 *
 * 职责：
 * 1. /authors/：支持作者姓名/简介防抖搜索（含中文 IME 保护）、A-Z 分组及目录分页无刷切换；
 * 2. /authors/<slug>/：支持跨类型 Tab、角色芯片、全文/仅标题、日期范围、排序与千万级双模分页无刷切换；
 * 3. AbortController + 请求序号防止弱网乱序覆盖，history.pushState / popstate 恢复；
 * 4. 局部刷新后调用 window.MyblogUI.mount() 重新挂载公共组件。
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    // =========================================================================
    // 模式一：作者专栏大厅 (/authors/)
    // =========================================================================
    var dirForm = document.getElementById('author-directory-form');
    var dirDynamic = document.getElementById('author-directory-dynamic');
    var dirInput = document.getElementById('author-directory-q');

    if (dirForm && dirDynamic) {
      var dirTimer = null;
      var dirAbort = null;
      var dirSeq = 0;
      var dirComposing = false;

      function fetchDirectory(url, pushState) {
        if (dirAbort) dirAbort.abort();
        dirAbort = new AbortController();
        var seq = ++dirSeq;
        dirDynamic.style.opacity = '0.55';
        dirDynamic.style.pointerEvents = 'none';

        fetch(url, {
          headers: {
            'X-Listing-Response': 'fragment-v1',
            'X-Requested-With': 'XMLHttpRequest',
          },
          signal: dirAbort.signal,
        })
          .then(function (resp) {
            var ct = resp.headers.get('Content-Type') || '';
            if (ct.indexOf('application/json') === -1) {
              throw new Error('Unexpected response: ' + resp.status);
            }
            return resp.json();
          })
          .then(function (data) {
            if (seq !== dirSeq) return;
            dirDynamic.style.opacity = '1';
            dirDynamic.style.pointerEvents = 'auto';
            if (typeof data.html === 'string') {
              dirDynamic.innerHTML = data.html;
            }
            if (pushState) {
              var targetHref = data.full_url
                ? new URL(data.full_url, window.location.origin).href
                : new URL(url, window.location.origin).href;
              window.history.pushState({ path: targetHref }, '', targetHref);
            }
          })
          .catch(function (err) {
            if (err && err.name === 'AbortError') return;
            dirDynamic.style.opacity = '1';
            dirDynamic.style.pointerEvents = 'auto';
          });
      }

      function buildDirUrl() {
        var fd = new FormData(dirForm);
        var params = new URLSearchParams();
        fd.forEach(function (v, k) {
          if (v !== '' && v !== null) params.append(k, v);
        });
        var base = dirForm.getAttribute('action') || window.location.pathname;
        var qs = params.toString();
        return qs ? base + '?' + qs : base;
      }

      dirForm.addEventListener('submit', function (e) {
        e.preventDefault();
        clearTimeout(dirTimer);
        fetchDirectory(buildDirUrl(), true);
      });

      if (dirInput) {
        dirInput.addEventListener('compositionstart', function () { dirComposing = true; });
        dirInput.addEventListener('compositionend', function () {
          dirComposing = false;
          clearTimeout(dirTimer);
          dirTimer = setTimeout(function () { fetchDirectory(buildDirUrl(), true); }, 250);
        });
        dirInput.addEventListener('input', function () {
          if (dirComposing) return;
          clearTimeout(dirTimer);
          dirTimer = setTimeout(function () { fetchDirectory(buildDirUrl(), true); }, 280);
        });
      }

      document.addEventListener('click', function (e) {
        var link = e.target.closest('.author-ajax-link');
        if (!link) return;
        if (!dirDynamic.contains(link) && !dirForm.contains(link)) return;
        e.preventDefault();
        var href = link.getAttribute('href');
        if (href) {
          if (link.id === 'author-directory-reset' && dirInput) {
            dirInput.value = '';
          }
          fetchDirectory(href, true);
        }
      });

      window.addEventListener('popstate', function () {
        fetchDirectory(window.location.href, false);
      });
      return;
    }

    // =========================================================================
    // 模式二：作者个人专栏 (/authors/<slug>/)
    // =========================================================================
    var detailForm = document.getElementById('author-detail-form');
    var detailDynamic = document.getElementById('author-detail-dynamic');
    var qInput = document.getElementById('author-detail-q');
    var titleOnlyCheck = document.getElementById('author-title-only');
    var sortSelect = document.getElementById('author-detail-sort');
    var dateFromInput = document.getElementById('author-date-from');
    var dateToInput = document.getElementById('author-date-to');
    var preservedBox = document.getElementById('author-preserved-params');
    var resetBtn = document.getElementById('author-detail-reset');

    if (!detailForm || !detailDynamic) {
      return;
    }

    var detailTimer = null;
    var detailAbort = null;
    var detailSeq = 0;
    var isSyncing = false;
    var isComposing = false;
    var userExplicitSort = Boolean(new URLSearchParams(window.location.search).get('sort'));

    function appendHidden(name, val) {
      if (!preservedBox || !val || val === 'all') return;
      var inp = document.createElement('input');
      inp.type = 'hidden';
      inp.name = name;
      inp.value = String(val);
      preservedBox.appendChild(inp);
    }

    function syncDetailForm(af) {
      if (!af) return;
      isSyncing = true;
      try {
        if (qInput && document.activeElement !== qInput) {
          qInput.value = af.q || '';
        }
        if (titleOnlyCheck) {
          titleOnlyCheck.checked = Boolean(af.title_only);
        }
        userExplicitSort = Boolean(af.explicit_sort);
        if (sortSelect && af.sort) {
          if (sortSelect.tomselect) {
            sortSelect.tomselect.setValue(af.sort, true);
          } else {
            sortSelect.value = af.sort;
          }
        }
        if (dateFromInput) {
          if (dateFromInput._flatpickr) {
            dateFromInput._flatpickr.setDate(af.date_from || null, false);
          } else {
            dateFromInput.value = af.date_from || '';
          }
        }
        if (dateToInput) {
          if (dateToInput._flatpickr) {
            dateToInput._flatpickr.setDate(af.date_to || null, false);
          } else {
            dateToInput.value = af.date_to || '';
          }
        }
        if (preservedBox) {
          preservedBox.innerHTML = '';
          appendHidden('type', af.type);
          appendHidden('role', af.role);
        }
      } finally {
        isSyncing = false;
      }
    }

    function fetchDetail(url, pushState) {
      if (detailAbort) detailAbort.abort();
      detailAbort = new AbortController();
      var seq = ++detailSeq;
      detailDynamic.style.opacity = '0.55';
      detailDynamic.style.pointerEvents = 'none';

      fetch(url, {
        headers: {
          'X-Listing-Response': 'fragment-v1',
          'X-Requested-With': 'XMLHttpRequest',
        },
        signal: detailAbort.signal,
      })
        .then(function (resp) {
          var ct = resp.headers.get('Content-Type') || '';
          if (ct.indexOf('application/json') === -1) {
            throw new Error('Unexpected response: ' + resp.status);
          }
          return resp.json().then(function (data) {
            return { ok: resp.ok, status: resp.status, data: data };
          });
        })
        .then(function (res) {
          if (seq !== detailSeq) return;
          detailDynamic.style.opacity = '1';
          detailDynamic.style.pointerEvents = 'auto';
          var data = res.data || {};
          if (!res.ok && res.status === 400 && data.error_message) {
            var alertBox = document.createElement('div');
            alertBox.className = 'alert alert-warning mb-3';
            alertBox.setAttribute('role', 'alert');
            alertBox.textContent = String(data.error_message);
            detailDynamic.innerHTML = '';
            detailDynamic.appendChild(alertBox);
          } else if (typeof data.html === 'string') {
            detailDynamic.innerHTML = data.html;
            if (window.MyblogUI && typeof window.MyblogUI.mount === 'function') {
              window.MyblogUI.mount(detailDynamic);
            }
          }
          if (res.ok && data.active_filters) {
            syncDetailForm(data.active_filters);
          }
          if (res.ok && pushState) {
            var targetHref = data.full_url
              ? new URL(data.full_url, window.location.origin).href
              : new URL(url, window.location.origin).href;
            window.history.pushState({ path: targetHref }, '', targetHref);
          }
        })
        .catch(function (err) {
          if (err && err.name === 'AbortError') return;
          detailDynamic.style.opacity = '1';
          detailDynamic.style.pointerEvents = 'auto';
        });
    }

    function buildDetailUrl(overrides) {
      var fd = new FormData(detailForm);
      var curParams = new URLSearchParams(window.location.search);
      var ps = curParams.get('page_size');
      if (ps && !fd.has('page_size')) fd.set('page_size', ps);

      if (!userExplicitSort) {
        fd.delete('sort');
      }
      fd.delete('cursor');
      fd.delete('page');
      fd.delete('seek_date');
      fd.delete('seek_direction');

      if (overrides) {
        Object.keys(overrides).forEach(function (k) {
          if (overrides[k] === null || overrides[k] === '') fd.delete(k);
          else fd.set(k, overrides[k]);
        });
      }

      var params = new URLSearchParams();
      fd.forEach(function (v, k) {
        if (v !== '' && v !== null) params.append(k, v);
      });
      var base = detailForm.getAttribute('action') || window.location.pathname;
      var qs = params.toString();
      return qs ? base + '?' + qs : base;
    }

    function buildUrlWithPaginationOverrides(overrides) {
      var url = new URL(window.location.href);
      Object.keys(overrides).forEach(function (k) {
        if (overrides[k] === null || overrides[k] === '') url.searchParams.delete(k);
        else url.searchParams.set(k, overrides[k]);
      });
      return url.pathname + url.search;
    }

    detailForm.addEventListener('submit', function (e) {
      e.preventDefault();
      clearTimeout(detailTimer);
      fetchDetail(buildDetailUrl(), true);
    });

    if (qInput) {
      qInput.addEventListener('compositionstart', function () { isComposing = true; });
      qInput.addEventListener('compositionend', function () {
        isComposing = false;
        if (isSyncing) return;
        clearTimeout(detailTimer);
        detailTimer = setTimeout(function () { fetchDetail(buildDetailUrl(), true); }, 250);
      });
      qInput.addEventListener('input', function () {
        if (isSyncing || isComposing) return;
        clearTimeout(detailTimer);
        detailTimer = setTimeout(function () { fetchDetail(buildDetailUrl(), true); }, 300);
      });
    }

    if (titleOnlyCheck) {
      titleOnlyCheck.addEventListener('change', function () {
        if (isSyncing) return;
        fetchDetail(buildDetailUrl(), true);
      });
    }
    if (sortSelect) {
      sortSelect.addEventListener('change', function () {
        if (isSyncing) return;
        userExplicitSort = true;
        fetchDetail(buildDetailUrl(), true);
      });
    }
    [dateFromInput, dateToInput].forEach(function (el) {
      if (!el) return;
      el.addEventListener('change', function () {
        if (isSyncing) return;
        fetchDetail(buildDetailUrl(), true);
      });
    });

    if (resetBtn) {
      resetBtn.addEventListener('click', function (e) {
        e.preventDefault();
        userExplicitSort = false;
        fetchDetail(resetBtn.getAttribute('href') || window.location.pathname, true);
      });
    }

    detailDynamic.addEventListener('click', function (e) {
      var ajaxLink = e.target.closest('.author-ajax-link');
      var nextBtn = e.target.closest('.listing-page-next-btn');
      var prevBtn = e.target.closest('.listing-page-prev-btn');
      var pageNumBtn = e.target.closest('.listing-page-num-btn');
      var oldestBtn = e.target.closest('.listing-oldest-btn');

      if (ajaxLink) {
        e.preventDefault();
        if (ajaxLink.getAttribute('data-author-reset') === '1') {
          userExplicitSort = false;
        }
        var href = ajaxLink.getAttribute('href');
        if (href) fetchDetail(href, true);
      } else if (nextBtn) {
        e.preventDefault();
        var np = nextBtn.getAttribute('data-page');
        var nc = nextBtn.getAttribute('data-cursor');
        if (np) fetchDetail(buildUrlWithPaginationOverrides({ page: np, cursor: null, seek_date: null, seek_direction: null }), true);
        else if (nc) fetchDetail(buildUrlWithPaginationOverrides({ cursor: nc, page: null, seek_date: null, seek_direction: null }), true);
      } else if (prevBtn) {
        e.preventDefault();
        var pp = prevBtn.getAttribute('data-page');
        var pc = prevBtn.getAttribute('data-cursor');
        if (pp) fetchDetail(buildUrlWithPaginationOverrides({ page: pp, cursor: null, seek_date: null, seek_direction: null }), true);
        else if (pc) fetchDetail(buildUrlWithPaginationOverrides({ cursor: pc, page: null, seek_date: null, seek_direction: null }), true);
      } else if (pageNumBtn) {
        e.preventDefault();
        var pv = pageNumBtn.getAttribute('data-page');
        if (pv) fetchDetail(buildUrlWithPaginationOverrides({ page: pv, cursor: null, seek_date: null, seek_direction: null }), true);
      } else if (oldestBtn) {
        e.preventDefault();
        fetchDetail(buildUrlWithPaginationOverrides({ seek_direction: 'oldest', cursor: null, page: null, seek_date: null }), true);
      }
    });

    detailDynamic.addEventListener('submit', function (e) {
      var jumpForm = e.target.closest('.listing-jump-page-form');
      var seekForm = e.target.closest('.listing-seek-date-form');
      if (jumpForm) {
        e.preventDefault();
        var pInp = jumpForm.querySelector('input[name="page"]');
        var pVal = pInp ? pInp.value.trim() : '';
        if (pVal) fetchDetail(buildUrlWithPaginationOverrides({ page: pVal, cursor: null, seek_date: null, seek_direction: null }), true);
      } else if (seekForm) {
        e.preventDefault();
        var dInp = seekForm.querySelector('input[name="seek_date"]');
        var dVal = dInp ? dInp.value.trim() : '';
        if (dVal) {
          var overrides = { seek_date: dVal, cursor: null, page: null, seek_direction: null };
          if (!new URLSearchParams(window.location.search).get('sort') || sortSelect.value === 'relevance') {
            overrides.sort = 'authored_desc';
            userExplicitSort = true;
          }
          fetchDetail(buildUrlWithPaginationOverrides(overrides), true);
        }
      }
    });

    window.addEventListener('popstate', function () {
      userExplicitSort = Boolean(new URLSearchParams(window.location.search).get('sort'));
      fetchDetail(window.location.href, false);
    });
  });
})();
