/**
 * 全站标签大厅与跨类型检索前端控制器 (TagController)
 * 落实事件委托驱动：
 * 1. 标签大厅 (/tags/): A–Z 字母导航、方案 B 展开、排序/视图切换、60/页分页跳转与 260ms 防抖服务端检索；
 * 2. 标签详情 (/tags/<slug>/): 多类型分栏 Tab、混合筛选与游标/页码分页全部无刷同步 history.pushState。
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    const dynamicArea = document.getElementById('tag-dynamic-content-area');
    if (!dynamicArea) {
      return;
    }

    let abortCtrl = null;
    let searchDebounceTimer = null;

    function buildUrlFromForm(form, extraOverrides) {
      const formData = new FormData(form);
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
        const trimmed = typeof value === 'string' ? value.trim() : value;
        if (trimmed !== '' && trimmed !== null) {
          params.append(key, trimmed);
        }
      }
      const baseUrl = form.getAttribute('action') || window.location.pathname;
      const qs = params.toString();
      return qs ? `${baseUrl}?${qs}` : baseUrl;
    }

    function buildDetailUrl(tagForm, newCursor, extraOverrides) {
      const overrides = Object.assign({}, extraOverrides || {});
      if (newCursor) {
        overrides.cursor = newCursor;
      } else {
        overrides.cursor = null;
      }
      return buildUrlFromForm(tagForm, overrides);
    }

    function fetchPartial(url, pushState, options) {
      const opts = options || {};
      if (abortCtrl) {
        abortCtrl.abort();
      }
      abortCtrl = new AbortController();

      let savedSelStart = null;
      let savedSelEnd = null;
      const activeEl = document.activeElement;
      const wasSearchFocused = Boolean(
        opts.restoreSearchFocus && activeEl && activeEl.id === 'tag-directory-filter-input'
      );
      if (wasSearchFocused && typeof activeEl.selectionStart === 'number') {
        savedSelStart = activeEl.selectionStart;
        savedSelEnd = activeEl.selectionEnd;
      }

      dynamicArea.style.opacity = '0.55';
      dynamicArea.style.pointerEvents = 'none';

      fetch(url, {
        headers: {
          'X-Listing-Response': 'fragment-v1',
          'X-Requested-With': 'XMLHttpRequest',
        },
        signal: abortCtrl.signal,
      })
        .then(function (res) {
          if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
          }
          return res.json();
        })
        .then(function (data) {
          dynamicArea.style.opacity = '1';
          dynamicArea.style.pointerEvents = 'auto';

          if (data && typeof data.html === 'string') {
            dynamicArea.innerHTML = data.html;
          }
          if (window.MyblogUI && typeof window.MyblogUI.mount === 'function') {
            window.MyblogUI.mount(dynamicArea);
          }

          if (pushState) {
            const targetUrl = new URL(url, window.location.origin);
            window.history.pushState({ path: targetUrl.href }, '', targetUrl.href);
          }

          if (wasSearchFocused) {
            const nextInput = document.getElementById('tag-directory-filter-input');
            if (nextInput) {
              nextInput.focus({ preventScroll: true });
              if (typeof savedSelStart === 'number' && typeof nextInput.setSelectionRange === 'function') {
                const len = nextInput.value.length;
                nextInput.setSelectionRange(Math.min(savedSelStart, len), Math.min(savedSelEnd, len));
              }
            }
          }
        })
        .catch(function (err) {
          if (err && err.name === 'AbortError') {
            return;
          }
          dynamicArea.style.opacity = '1';
          dynamicArea.style.pointerEvents = 'auto';
          console.error('Tag partial fetch failed:', err);
        });
    }

    // 统一点击事件委托（标签大厅导航/分页/排序/视图 + 标签详情类型Tab/游标分页）
    dynamicArea.addEventListener('click', function (e) {
      const dirNavLink = e.target.closest('.tag-dir-nav-link');
      if (dirNavLink) {
        e.preventDefault();
        const href = dirNavLink.getAttribute('href');
        if (href) {
          fetchPartial(href, true);
        }
        return;
      }

      const tabLink = e.target.closest('#tag-type-tabs .tag-tab-link, #tag-type-tabs [data-type]');
      if (tabLink) {
        e.preventDefault();
        const tagForm = document.getElementById('tag-filter-form');
        const activeTypeInput = document.getElementById('tag-active-type-input');
        const tagCursorInput = document.getElementById('tag-cursor-input');
        const typeSlug = tabLink.getAttribute('data-type');
        if (activeTypeInput && typeSlug) {
          activeTypeInput.value = typeSlug;
        }
        if (tagCursorInput) {
          tagCursorInput.value = '';
        }
        if (tagForm) {
          const url = buildDetailUrl(tagForm, null);
          fetchPartial(url, true);
        }
        return;
      }

      const tagForm = document.getElementById('tag-filter-form');
      if (!tagForm) {
        return;
      }

      const nextBtn = e.target.closest('.listing-page-next-btn');
      const prevBtn = e.target.closest('.listing-page-prev-btn');
      const pageNumBtn = e.target.closest('.listing-page-num-btn');
      const oldestBtn = e.target.closest('.listing-oldest-btn');

      if (nextBtn) {
        e.preventDefault();
        const pageVal = nextBtn.getAttribute('data-page');
        const cursorVal = nextBtn.getAttribute('data-cursor');
        if (pageVal) {
          fetchPartial(buildDetailUrl(tagForm, null, { page: pageVal, seek_date: null, seek_direction: null }), true);
        } else if (cursorVal) {
          fetchPartial(buildDetailUrl(tagForm, cursorVal), true);
        }
      } else if (prevBtn) {
        e.preventDefault();
        const pageVal = prevBtn.getAttribute('data-page');
        const cursorVal = prevBtn.getAttribute('data-cursor');
        if (pageVal) {
          fetchPartial(buildDetailUrl(tagForm, null, { page: pageVal, seek_date: null, seek_direction: null }), true);
        } else if (cursorVal) {
          fetchPartial(buildDetailUrl(tagForm, cursorVal), true);
        }
      } else if (pageNumBtn) {
        e.preventDefault();
        const pageVal = pageNumBtn.getAttribute('data-page');
        if (pageVal) {
          fetchPartial(buildDetailUrl(tagForm, null, { page: pageVal, seek_date: null, seek_direction: null }), true);
        }
      } else if (oldestBtn) {
        e.preventDefault();
        fetchPartial(buildDetailUrl(tagForm, null, { seek_direction: 'oldest', page: null, seek_date: null }), true);
      }
    });

    // 统一表单提交委托（大厅搜索/页码跳转 + 详情检索/跳转）
    dynamicArea.addEventListener('submit', function (e) {
      const dirForm = e.target.closest('[data-tag-dir-form]');
      if (dirForm) {
        e.preventDefault();
        if (searchDebounceTimer) {
          clearTimeout(searchDebounceTimer);
          searchDebounceTimer = null;
        }
        fetchPartial(buildUrlFromForm(dirForm, { page: null }), true);
        return;
      }

      const dirJumpForm = e.target.closest('.tag-jump-form');
      if (dirJumpForm) {
        e.preventDefault();
        fetchPartial(buildUrlFromForm(dirJumpForm), true);
        return;
      }

      const tagForm = document.getElementById('tag-filter-form');
      const tagResultsContainer = document.getElementById('tag-results-container');
      if (!tagForm) {
        return;
      }

      if (e.target === tagForm) {
        e.preventDefault();
        const tagCursorInput = document.getElementById('tag-cursor-input');
        if (tagCursorInput) {
          tagCursorInput.value = '';
        }
        fetchPartial(buildDetailUrl(tagForm, null), true);
        return;
      }

      const jumpForm = e.target.closest('.listing-jump-page-form');
      const seekForm = e.target.closest('.listing-seek-date-form');
      if (jumpForm && tagResultsContainer && tagResultsContainer.contains(jumpForm)) {
        e.preventDefault();
        const pInput = jumpForm.querySelector('input[name="page"]');
        const pVal = pInput ? pInput.value.trim() : '';
        if (pVal) {
          fetchPartial(buildDetailUrl(tagForm, null, { page: pVal, seek_date: null, seek_direction: null }), true);
        }
      } else if (seekForm && tagResultsContainer && tagResultsContainer.contains(seekForm)) {
        e.preventDefault();
        const dInput = seekForm.querySelector('input[name="seek_date"]');
        const dVal = dInput ? dInput.value.trim() : '';
        if (dVal) {
          fetchPartial(buildDetailUrl(tagForm, null, { seek_date: dVal, page: null, seek_direction: null }), true);
        }
      }
    });

    // 标签大厅 260ms 防抖服务端搜索
    dynamicArea.addEventListener('input', function (e) {
      const dirInput = e.target.closest('#tag-directory-filter-input');
      if (!dirInput) {
        return;
      }
      const dirForm = dirInput.closest('[data-tag-dir-form]');
      if (!dirForm) {
        return;
      }
      if (searchDebounceTimer) {
        clearTimeout(searchDebounceTimer);
      }
      searchDebounceTimer = setTimeout(function () {
        searchDebounceTimer = null;
        fetchPartial(buildUrlFromForm(dirForm, { page: null }), true, { restoreSearchFocus: true });
      }, 260);
    });

    // 大厅字母范围复选框 & 详情筛选控件变更委托
    dynamicArea.addEventListener('change', function (e) {
      const letterScopeCheck = e.target.closest('[data-tag-letter-scope]');
      if (letterScopeCheck) {
        const dirForm = letterScopeCheck.closest('[data-tag-dir-form]');
        if (dirForm) {
          fetchPartial(buildUrlFromForm(dirForm, { page: null }), true);
        }
        return;
      }

      const tagForm = document.getElementById('tag-filter-form');
      if (tagForm && tagForm.contains(e.target) && e.target.matches('select, input[type="checkbox"], input[type="date"], input[data-ui-date]')) {
        const tagCursorInput = document.getElementById('tag-cursor-input');
        if (tagCursorInput) {
          tagCursorInput.value = '';
        }
        fetchPartial(buildDetailUrl(tagForm, null), true);
      }
    });

    // 浏览器前进/后退
    window.addEventListener('popstate', function () {
      fetchPartial(window.location.href, false);
    });
  });
})();
