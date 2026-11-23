/**
 * 全站统一搜索与多维组合筛选前端控制器 (SearchController)
 *
 * 职责：
 * 1. 首屏 SSR 完整直出，保留原生链接与表单降级能力；
 * 2. 拦截搜索提交、关键词防抖、筛选变化、分面切换、页码/游标/日期定位并走 AJAX 局部更新；
 * 3. 同步更新浏览器历史栈 (history.pushState / popstate) 与顶部表单状态；
 * 4. 局部刷新后调用 window.MyblogUI.mount() 重新挂载 Flatpickr 等公共组件；
 * 5. 焦点保护：搜索输入框常驻容器外部，输入期间绝不重建或覆盖焦点。
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var form = document.getElementById('search-filter-form');
    var dynamicContainer = document.getElementById('search-dynamic-container');
    var queryInput = document.getElementById('search-query-input');
    var titleOnlyCheck = document.getElementById('search-title-only-check');
    var sortSelect = document.getElementById('search-sort-select');
    var authorSelect = document.getElementById('search-author-select');
    var authorRoleSelect = document.getElementById('search-author-role-select');
    var dateFromInput = document.getElementById('search-date-from');
    var dateToInput = document.getElementById('search-date-to');
    var resetBtn = document.getElementById('search-reset-btn');
    var preservedContainer = document.getElementById('search-preserved-params');

    if (!form || !dynamicContainer) {
      return;
    }

    var debounceTimer = null;
    var activeAbortController = null;
    var isSyncingForm = false;
    var userExplicitSort = Boolean(new URLSearchParams(window.location.search).get('sort'));

    function appendHiddenInput(name, value) {
      if (!preservedContainer || value === null || value === undefined || value === '') return;
      var inp = document.createElement('input');
      inp.type = 'hidden';
      inp.name = name;
      inp.value = String(value);
      preservedContainer.appendChild(inp);
    }

    function syncFormWithState(activeFilters) {
      if (!activeFilters) return;
      isSyncingForm = true;
      try {
        if (queryInput && document.activeElement !== queryInput) {
          queryInput.value = activeFilters.query || '';
        }
        if (titleOnlyCheck) {
          titleOnlyCheck.checked = Boolean(activeFilters.title_only);
        }
        userExplicitSort = Boolean(activeFilters.explicit_sort);
        if (sortSelect && activeFilters.sort) {
          if (sortSelect.tomselect) {
            sortSelect.tomselect.setValue(activeFilters.sort, true);
          } else {
            sortSelect.value = activeFilters.sort;
          }
        }
        if (authorSelect) {
          var nextAuthors = Array.isArray(activeFilters.authors) ? activeFilters.authors : [];
          var authorOpts = Array.isArray(activeFilters.author_options) ? activeFilters.author_options : [];
          if (authorSelect.tomselect) {
            authorOpts.forEach(function (opt) {
              if (opt && opt.slug && !authorSelect.tomselect.options[opt.slug]) {
                authorSelect.tomselect.addOption({ value: opt.slug, label: opt.label || opt.slug });
              }
            });
            authorSelect.tomselect.setValue(nextAuthors, true);
          } else {
            Array.from(authorSelect.options).forEach(function (optEl) {
              optEl.selected = nextAuthors.indexOf(optEl.value) !== -1;
            });
          }
        }
        if (authorRoleSelect) {
          var nextRole = activeFilters.author_role || '';
          if (authorRoleSelect.tomselect) {
            authorRoleSelect.tomselect.setValue(nextRole, true);
          } else {
            authorRoleSelect.value = nextRole;
          }
        }
        if (dateFromInput) {
          var dfVal = activeFilters.date_from || '';
          if (dateFromInput._flatpickr) {
            dateFromInput._flatpickr.setDate(dfVal || null, false);
          } else {
            dateFromInput.value = dfVal;
          }
        }
        if (dateToInput) {
          var dtVal = activeFilters.date_to || '';
          if (dateToInput._flatpickr) {
            dateToInput._flatpickr.setDate(dtVal || null, false);
          } else {
            dateToInput.value = dtVal;
          }
        }
        if (preservedContainer) {
          preservedContainer.innerHTML = '';
          appendHiddenInput('type', activeFilters.type);
          appendHiddenInput('year', activeFilters.year);
          appendHiddenInput('days', activeFilters.days);
          if (Array.isArray(activeFilters.tags)) {
            activeFilters.tags.forEach(function (t) {
              appendHiddenInput('tag', t);
            });
          }
        }
      } finally {
        isSyncingForm = false;
      }
    }

    function fetchSearch(url, pushState) {
      if (activeAbortController) {
        activeAbortController.abort();
      }
      activeAbortController = new AbortController();

      dynamicContainer.style.opacity = '0.55';
      dynamicContainer.style.pointerEvents = 'none';

      fetch(url, {
        headers: {
          'X-Listing-Response': 'fragment-v1',
          'X-Requested-With': 'XMLHttpRequest',
        },
        signal: activeAbortController.signal,
      })
        .then(function (resp) {
          var contentType = resp.headers.get('Content-Type') || '';
          if (contentType.indexOf('application/json') === -1) {
            throw new Error('Unexpected response type: ' + resp.status);
          }
          return resp.json().then(function (data) {
            return { status: resp.status, data: data };
          });
        })
        .then(function (res) {
          dynamicContainer.style.opacity = '1';
          dynamicContainer.style.pointerEvents = 'auto';

          var data = res.data || {};
          if (typeof data.html === 'string') {
            dynamicContainer.innerHTML = data.html;
            if (window.MyblogUI && typeof window.MyblogUI.mount === 'function') {
              window.MyblogUI.mount(dynamicContainer);
            }
          }
          if (data.active_filters) {
            syncFormWithState(data.active_filters);
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
          dynamicContainer.style.opacity = '1';
          dynamicContainer.style.pointerEvents = 'auto';
          console.error('Search fetch failed:', err);
        });
    }

    function buildUrlFromForm(overrides) {
      var formData = new FormData(form);
      var curParams = new URLSearchParams(window.location.search);
      var curPs = curParams.get('page_size');
      if (curPs && !formData.has('page_size')) {
        formData.set('page_size', curPs);
      }
      var curExpand = curParams.get('expand');
      if (curExpand && !formData.has('expand')) {
        formData.set('expand', curExpand);
      }

      if (!userExplicitSort) {
        formData.delete('sort');
      }

      formData.delete('cursor');
      formData.delete('page');
      formData.delete('seek_date');
      formData.delete('seek_direction');

      if (overrides) {
        Object.keys(overrides).forEach(function (k) {
          if (overrides[k] === null || overrides[k] === '') {
            formData.delete(k);
          } else {
            formData.set(k, overrides[k]);
          }
        });
      }

      var params = new URLSearchParams();
      formData.forEach(function (value, key) {
        if (value !== '' && value !== null) {
          params.append(key, value);
        }
      });

      var baseUrl = form.getAttribute('action') || window.location.pathname;
      var qs = params.toString();
      return qs ? baseUrl + '?' + qs : baseUrl;
    }

    function buildUrlWithOverrides(overrides) {
      var url = new URL(window.location.href);
      Object.keys(overrides).forEach(function (k) {
        if (overrides[k] === null || overrides[k] === '') {
          url.searchParams.delete(k);
        } else {
          url.searchParams.set(k, overrides[k]);
        }
      });
      return url.pathname + url.search;
    }

    // 1. 顶部搜索表单提交
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      clearTimeout(debounceTimer);
      fetchSearch(buildUrlFromForm(), true);
    });

    // 2. 搜索框 300ms 防抖自动检索（保护中文输入法与焦点）
    if (queryInput) {
      queryInput.addEventListener('input', function () {
        if (isSyncingForm) return;
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () {
          fetchSearch(buildUrlFromForm(), true);
        }, 300);
      });
    }

    // 3. 仅搜标题、排序、日期范围变动即时刷新
    if (titleOnlyCheck) {
      titleOnlyCheck.addEventListener('change', function () {
        if (isSyncingForm) return;
        fetchSearch(buildUrlFromForm(), true);
      });
    }
    if (sortSelect) {
      sortSelect.addEventListener('change', function () {
        if (isSyncingForm) return;
        userExplicitSort = true;
        fetchSearch(buildUrlFromForm(), true);
      });
    }
    if (authorSelect) {
      authorSelect.addEventListener('change', function () {
        if (isSyncingForm) return;
        fetchSearch(buildUrlFromForm(), true);
      });
    }
    if (authorRoleSelect) {
      authorRoleSelect.addEventListener('change', function () {
        if (isSyncingForm) return;
        fetchSearch(buildUrlFromForm(), true);
      });
    }
    [dateFromInput, dateToInput].forEach(function (dateEl) {
      if (!dateEl) return;
      dateEl.addEventListener('change', function () {
        if (isSyncingForm) return;
        fetchSearch(buildUrlFromForm({ days: null }), true);
      });
    });

    // 4. 顶部重置按钮
    if (resetBtn) {
      resetBtn.addEventListener('click', function (e) {
        e.preventDefault();
        userExplicitSort = false;
        fetchSearch(resetBtn.getAttribute('href') || window.location.pathname, true);
      });
    }

    // 5. 动态区域内点击委托（分面、标签、年份、快捷时间、移除胶囊、分页与跳页）
    dynamicContainer.addEventListener('click', function (e) {
      var ajaxLink = e.target.closest('.search-ajax-link');
      var nextBtn = e.target.closest('.listing-page-next-btn');
      var prevBtn = e.target.closest('.listing-page-prev-btn');
      var pageNumBtn = e.target.closest('.listing-page-num-btn');
      var oldestBtn = e.target.closest('.listing-oldest-btn');

      if (ajaxLink) {
        e.preventDefault();
        if (ajaxLink.getAttribute('data-search-reset') === '1') {
          userExplicitSort = false;
        }
        var href = ajaxLink.getAttribute('href');
        if (href) {
          fetchSearch(href, true);
        }
      } else if (nextBtn) {
        e.preventDefault();
        var nextCursor = nextBtn.getAttribute('data-cursor');
        if (nextCursor) {
          fetchSearch(
            buildUrlWithOverrides({ cursor: nextCursor, page: null, seek_date: null, seek_direction: null }),
            true
          );
        }
      } else if (prevBtn) {
        e.preventDefault();
        var prevCursor = prevBtn.getAttribute('data-cursor');
        if (prevCursor) {
          fetchSearch(
            buildUrlWithOverrides({ cursor: prevCursor, page: null, seek_date: null, seek_direction: null }),
            true
          );
        }
      } else if (pageNumBtn) {
        e.preventDefault();
        var pageVal = pageNumBtn.getAttribute('data-page');
        if (pageVal) {
          fetchSearch(
            buildUrlWithOverrides({ page: pageVal, cursor: null, seek_date: null, seek_direction: null }),
            true
          );
        }
      } else if (oldestBtn) {
        e.preventDefault();
        fetchSearch(
          buildUrlWithOverrides({ seek_direction: 'oldest', cursor: null, page: null, seek_date: null }),
          true
        );
      }
    });

    // 6. 分页区跳页与日期定位表单委托提交
    dynamicContainer.addEventListener('submit', function (e) {
      var jumpForm = e.target.closest('.listing-jump-page-form');
      var seekForm = e.target.closest('.listing-seek-date-form');
      if (jumpForm) {
        e.preventDefault();
        var pInput = jumpForm.querySelector('input[name="page"]');
        var pVal = pInput ? pInput.value.trim() : '';
        if (pVal) {
          fetchSearch(
            buildUrlWithOverrides({ page: pVal, cursor: null, seek_date: null, seek_direction: null }),
            true
          );
        }
      } else if (seekForm) {
        e.preventDefault();
        var dInput = seekForm.querySelector('input[name="seek_date"]');
        var dVal = dInput ? dInput.value.trim() : '';
        if (dVal) {
          fetchSearch(
            buildUrlWithOverrides({ seek_date: dVal, cursor: null, page: null, seek_direction: null }),
            true
          );
        }
      }
    });

    // 7. 浏览器前进 / 后退恢复
    window.addEventListener('popstate', function () {
      userExplicitSort = Boolean(new URLSearchParams(window.location.search).get('sort'));
      fetchSearch(window.location.href, false);
    });
  });
})();
