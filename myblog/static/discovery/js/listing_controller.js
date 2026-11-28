/**
 * Wagtail 全站层级列表与多维检索前端控制器 (ListingController)
 * 落实 D3-A: 首屏 SSR 保证 SEO，筛选与翻页走 AJAX 局部更新并同步 URL (history.pushState)
 * 焦点保护: 保持搜索输入框 (<input name="q">) 外部常驻，绝不重绘输入框，杜绝打断拼音输入法与失焦。
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    const form = document.getElementById('listing-filter-form');
    const resultsContainer = document.getElementById('listing-results-container');
    const queryInput = document.getElementById('listing-query-input');
    const cursorInput = document.getElementById('listing-cursor-input');

    if (!form || !resultsContainer) {
      return;
    }

    let debounceTimer = null;
    let activeAbortController = null;

    function fetchListing(url, pushState = true) {
      if (activeAbortController) {
        activeAbortController.abort();
      }
      activeAbortController = new AbortController();

      // 添加加载状态半透明提示
      resultsContainer.style.opacity = '0.5';
      resultsContainer.style.pointerEvents = 'none';

      fetch(url, {
        headers: {
          'X-Listing-Response': 'fragment-v1',
          'X-Requested-With': 'XMLHttpRequest',
        },
        signal: activeAbortController.signal,
      })
        .then(response => {
          if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
          }
          return response.json();
        })
        .then(data => {
          resultsContainer.style.opacity = '1';
          resultsContainer.style.pointerEvents = 'auto';
          resultsContainer.innerHTML = data.html;
          if (window.MyblogUI && typeof window.MyblogUI.mount === 'function') {
            window.MyblogUI.mount(resultsContainer);
          }

          if (pushState && data.canonical_url) {
            const targetUrl = new URL(url, window.location.origin);
            window.history.pushState({ path: targetUrl.href }, '', targetUrl.href);
          }
        })
        .catch(err => {
          if (err.name === 'AbortError') return;
          resultsContainer.style.opacity = '1';
          resultsContainer.style.pointerEvents = 'auto';
          console.error('Listing fetch failed:', err);
        });
    }

    function buildUrlFromForm(newCursor = null, extraOverrides = null) {
      const formData = new FormData(form);
      const curPs = new URLSearchParams(window.location.search).get('page_size');
      if (curPs && !formData.has('page_size')) {
        formData.set('page_size', curPs);
      }
      if (newCursor) {
        formData.set('cursor', newCursor);
      } else {
        formData.delete('cursor');
      }
      if (extraOverrides) {
        Object.keys(extraOverrides).forEach(function (k) {
          if (extraOverrides[k] === null || extraOverrides[k] === '') {
            formData.delete(k);
          } else {
            formData.set(k, extraOverrides[k]);
          }
        });
      }

      const params = new URLSearchParams();
      for (const [key, value] of formData.entries()) {
        if (value !== '' && value !== null) {
          params.append(key, value);
        }
      }
      const baseUrl = form.getAttribute('action') || window.location.pathname;
      const qs = params.toString();
      return qs ? `${baseUrl}?${qs}` : baseUrl;
    }

    // 1. 表单提交（拦截回车与点击搜索）
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (cursorInput) cursorInput.value = '';
      const url = buildUrlFromForm(null);
      fetchListing(url, true);
    });

    // 2. 搜索输入框 300ms 防抖自动检索（绝不触发 DOM 替换以保护 IME 中文输入法）
    if (queryInput) {
      queryInput.addEventListener('input', function () {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () {
          if (cursorInput) cursorInput.value = '';
          const url = buildUrlFromForm(null);
          fetchListing(url, true);
        }, 300);
      });
    }

    // 3. 复选框与下拉筛选变动立即触发刷新
    form.querySelectorAll('input[type="checkbox"], select, input[type="date"], input[data-ui-date]').forEach(function (el) {
      el.addEventListener('change', function () {
        if (cursorInput) cursorInput.value = '';
        const url = buildUrlFromForm(null);
        fetchListing(url, true);
      });
    });

    // 4. 事件委托处理分页上一页 / 下一页 / 页码 / 最早一批
    document.addEventListener('click', function (e) {
      const nextBtn = e.target.closest('.listing-page-next-btn');
      const prevBtn = e.target.closest('.listing-page-prev-btn');
      const pageNumBtn = e.target.closest('.listing-page-num-btn');
      const oldestBtn = e.target.closest('.listing-oldest-btn');

      if (nextBtn) {
        e.preventDefault();
        const pageVal = nextBtn.getAttribute('data-page');
        const cursorVal = nextBtn.getAttribute('data-cursor');
        if (pageVal) {
          const url = buildUrlFromForm(null, { page: pageVal, seek_date: null, seek_direction: null });
          fetchListing(url, true);
        } else if (cursorVal) {
          const url = buildUrlFromForm(cursorVal);
          fetchListing(url, true);
        }
      } else if (prevBtn) {
        e.preventDefault();
        const pageVal = prevBtn.getAttribute('data-page');
        const cursorVal = prevBtn.getAttribute('data-cursor');
        if (pageVal) {
          const url = buildUrlFromForm(null, { page: pageVal, seek_date: null, seek_direction: null });
          fetchListing(url, true);
        } else if (cursorVal) {
          const url = buildUrlFromForm(cursorVal);
          fetchListing(url, true);
        }
      } else if (pageNumBtn) {
        e.preventDefault();
        const pageVal = pageNumBtn.getAttribute('data-page');
        if (pageVal) {
          const url = buildUrlFromForm(null, { page: pageVal, seek_date: null, seek_direction: null });
          fetchListing(url, true);
        }
      } else if (oldestBtn) {
        e.preventDefault();
        const url = buildUrlFromForm(null, { seek_direction: 'oldest', page: null, seek_date: null });
        fetchListing(url, true);
      }
    });

    // 4.5 分页区跳页与日期定位表单委托提交
    document.addEventListener('submit', function (e) {
      const jumpForm = e.target.closest('.listing-jump-page-form');
      const seekForm = e.target.closest('.listing-seek-date-form');
      if (jumpForm && resultsContainer.contains(jumpForm)) {
        e.preventDefault();
        const pInput = jumpForm.querySelector('input[name="page"]');
        const pVal = pInput ? pInput.value.trim() : '';
        if (pVal) {
          const url = buildUrlFromForm(null, { page: pVal, seek_date: null, seek_direction: null });
          fetchListing(url, true);
        }
      } else if (seekForm && resultsContainer.contains(seekForm)) {
        e.preventDefault();
        const dInput = seekForm.querySelector('input[name="seek_date"]');
        const dVal = dInput ? dInput.value.trim() : '';
        if (dVal) {
          const url = buildUrlFromForm(null, { seek_date: dVal, page: null, seek_direction: null });
          fetchListing(url, true);
        }
      }
    });

    // 5. 监听浏览器前进/后退 (popstate)
    window.addEventListener('popstate', function () {
      fetchListing(window.location.href, false);
    });
  });
})();
