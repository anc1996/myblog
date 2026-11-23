/**
 * 全站标签大厅与跨类型检索前端控制器 (TagController)
 * 落实 D2-A (类型分栏 Tab 无刷切换) 与 D3-A (局部更新 + history.pushState 同步)
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    // 1. 标签大厅即时名称搜索过滤 (模式一)
    const filterInput = document.getElementById('tag-directory-filter-input');
    if (filterInput) {
      filterInput.addEventListener('input', function () {
        const term = this.value.trim().toLowerCase();
        const tagCards = document.querySelectorAll('.tag-card-wrapper');
        const groupSections = document.querySelectorAll('.tag-group-section');

        tagCards.forEach(card => {
          const name = card.getAttribute('data-tag-name') || '';
          if (!term || name.includes(term)) {
            card.style.display = '';
          } else {
            card.style.display = 'none';
          }
        });

        // 隐藏空的分组
        groupSections.forEach(sec => {
          const visibleCards = sec.querySelectorAll('.tag-card-wrapper:not([style*="display: none"])');
          sec.style.display = visibleCards.length > 0 ? '' : 'none';
        });
      });
    }

    // 2. 标签详情页：多类型 Tab 无刷切换与表单 AJAX 交互 (模式二)
    const tagForm = document.getElementById('tag-filter-form');
    const tagResultsContainer = document.getElementById('tag-results-container');
    const activeTypeInput = document.getElementById('tag-active-type-input');
    const tagCursorInput = document.getElementById('tag-cursor-input');

    if (!tagForm || !tagResultsContainer) {
      return;
    }

    let abortCtrl = null;

    function fetchTagResults(url, pushState = true) {
      if (abortCtrl) abortCtrl.abort();
      abortCtrl = new AbortController();

      tagResultsContainer.style.opacity = '0.5';
      tagResultsContainer.style.pointerEvents = 'none';

      fetch(url, {
        headers: {
          'X-Listing-Response': 'fragment-v1',
          'X-Requested-With': 'XMLHttpRequest',
        },
        signal: abortCtrl.signal,
      })
        .then(res => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json();
        })
        .then(data => {
          tagResultsContainer.style.opacity = '1';
          tagResultsContainer.style.pointerEvents = 'auto';
          tagResultsContainer.innerHTML = data.html;
          if (window.MyblogUI && typeof window.MyblogUI.mount === 'function') {
            window.MyblogUI.mount(tagResultsContainer);
          }

          if (pushState && data.canonical_url) {
            const targetUrl = new URL(url, window.location.origin);
            window.history.pushState({ path: targetUrl.href }, '', targetUrl.href);
          }
        })
        .catch(err => {
          if (err.name === 'AbortError') return;
          tagResultsContainer.style.opacity = '1';
          tagResultsContainer.style.pointerEvents = 'auto';
          console.error('Tag results fetch failed:', err);
        });
    }

    function buildTagUrl(newCursor = null, extraOverrides = null) {
      const formData = new FormData(tagForm);
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
      const baseUrl = tagForm.getAttribute('action') || window.location.pathname;
      const qs = params.toString();
      return qs ? `${baseUrl}?${qs}` : baseUrl;
    }

    // 点击类型 Tab 无刷切换 (全部/博客/指南/项目)
    document.querySelectorAll('#tag-type-tabs .nav-link').forEach(tabLink => {
      tabLink.addEventListener('click', function (e) {
        e.preventDefault();
        const typeSlug = this.getAttribute('data-type');
        if (activeTypeInput) activeTypeInput.value = typeSlug;
        if (tagCursorInput) tagCursorInput.value = '';

        // 更新 Tab 高亮
        document.querySelectorAll('#tag-type-tabs .nav-link').forEach(l => {
          l.classList.remove('active', 'fw-bold', 'text-primary');
          l.classList.add('text-muted');
        });
        this.classList.add('active', 'fw-bold', 'text-primary');
        this.classList.remove('text-muted');

        const url = buildTagUrl(null);
        fetchTagResults(url, true);
      });
    });

    // 表单提交
    tagForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (tagCursorInput) tagCursorInput.value = '';
      const url = buildTagUrl(null);
      fetchTagResults(url, true);
    });

    // 排序与日期变动即时响应
    tagForm.querySelectorAll('select, input[type="checkbox"], input[type="date"], input[data-ui-date]').forEach(el => {
      el.addEventListener('change', function () {
        if (tagCursorInput) tagCursorInput.value = '';
        const url = buildTagUrl(null);
        fetchTagResults(url, true);
      });
    });

    // 分页上一页 / 下一页事件委托
    document.addEventListener('click', function (e) {
      const nextBtn = e.target.closest('.listing-page-next-btn');
      const prevBtn = e.target.closest('.listing-page-prev-btn');
      const pageNumBtn = e.target.closest('.listing-page-num-btn');
      const oldestBtn = e.target.closest('.listing-oldest-btn');
      if (nextBtn) {
        e.preventDefault();
        const cursorVal = nextBtn.getAttribute('data-cursor');
        if (cursorVal) {
          const url = buildTagUrl(cursorVal);
          fetchTagResults(url, true);
        }
      } else if (prevBtn) {
        e.preventDefault();
        const cursorVal = prevBtn.getAttribute('data-cursor');
        if (cursorVal) {
          const url = buildTagUrl(cursorVal);
          fetchTagResults(url, true);
        }
      } else if (pageNumBtn) {
        e.preventDefault();
        const pageVal = pageNumBtn.getAttribute('data-page');
        if (pageVal) {
          const url = buildTagUrl(null, { page: pageVal, seek_date: null, seek_direction: null });
          fetchTagResults(url, true);
        }
      } else if (oldestBtn) {
        e.preventDefault();
        const url = buildTagUrl(null, { seek_direction: 'oldest', page: null, seek_date: null });
        fetchTagResults(url, true);
      }
    });

    document.addEventListener('submit', function (e) {
      const jumpForm = e.target.closest('.listing-jump-page-form');
      const seekForm = e.target.closest('.listing-seek-date-form');
      if (jumpForm && tagResultsContainer.contains(jumpForm)) {
        e.preventDefault();
        const pInput = jumpForm.querySelector('input[name="page"]');
        const pVal = pInput ? pInput.value.trim() : '';
        if (pVal) {
          const url = buildTagUrl(null, { page: pVal, seek_date: null, seek_direction: null });
          fetchTagResults(url, true);
        }
      } else if (seekForm && tagResultsContainer.contains(seekForm)) {
        e.preventDefault();
        const dInput = seekForm.querySelector('input[name="seek_date"]');
        const dVal = dInput ? dInput.value.trim() : '';
        if (dVal) {
          const url = buildTagUrl(null, { seek_date: dVal, page: null, seek_direction: null });
          fetchTagResults(url, true);
        }
      }
    });

    // 浏览器前进/后退
    window.addEventListener('popstate', function () {
      fetchTagResults(window.location.href, false);
    });
  });
})();
