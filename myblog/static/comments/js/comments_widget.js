/**
 * 全站通用评论互动组件前端控制器 (CommentsWidget)
 * 落实 决策三-A: 轻量 AJAX 提交 + 友好免刷新回显 + 两层扁平回复 + 点赞互动
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    const form = document.getElementById('comment-post-form');
    const itemsList = document.getElementById('comments-items-list');
    const countBadge = document.getElementById('comments-count-badge');
    const loadingIndicator = document.getElementById('comments-loading-indicator');
    const formAlert = document.getElementById('comment-form-alert');
    const replyNotice = document.getElementById('comment-replying-notice');
    const replyTargetName = document.getElementById('replying-target-name');
    const cancelReplyBtn = document.getElementById('cancel-reply-btn');
    const parentIdInput = document.getElementById('comment-parent-id-input');
    const replyToIdInput = document.getElementById('comment-reply-to-id-input');
    const loadMoreBtn = document.getElementById('load-more-comments-btn');
    const paginationArea = document.getElementById('comments-pagination-area');

    if (!form || !itemsList) {
      return;
    }

    const pageId = form.getAttribute('data-page-id');
    const postUrl = form.getAttribute('data-post-url');
    const listUrl = form.getAttribute('data-list-url');

    let currentPage = 1;
    let totalPages = 1;

    function showAlert(msg, isSuccess = true) {
      formAlert.className = `alert alert-${isSuccess ? 'success' : 'danger'} py-2 small mb-3`;
      formAlert.textContent = msg;
      formAlert.style.display = 'block';
      setTimeout(() => {
        formAlert.style.display = 'none';
      }, 5000);
    }

    function renderCommentCard(c, isReply = false) {
      const card = document.createElement('div');
      card.className = `comment-card p-3 rounded border bg-white ${isReply ? 'ms-4 mt-2 border-start-3 border-start-primary' : 'shadow-sm'}`;
      card.id = `comment-${c.id}`;

      const authorName = c.author ? c.author.username : '匿名读者';
      const dateStr = c.created_at || '';

      card.innerHTML = `
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="d-flex align-items-center gap-2">
            <span class="fw-bold text-dark" style="font-size: 0.9rem;">${authorName}</span>
            <span class="text-muted" style="font-size: 0.75rem;">${dateStr}</span>
          </div>
          <button type="button" class="btn btn-sm btn-link text-primary p-0 text-decoration-none reply-trigger-btn"
                  data-id="${c.id}" data-author="${authorName}" style="font-size: 0.8rem;">
            回复
          </button>
        </div>
        <div class="comment-body-html text-secondary mb-2" style="font-size: 0.9rem; line-height: 1.5;">
          ${c.safe_html}
        </div>
        <div class="d-flex align-items-center gap-3">
          <button type="button" class="btn btn-sm btn-outline-light text-muted border-0 react-btn" data-id="${c.id}" data-action="1" style="font-size: 0.8rem;">
            👍 <span class="like-num">${c.like_count || 0}</span>
          </button>
        </div>
        <div class="replies-stream-container mt-2" id="replies-stream-${c.id}"></div>
      `;
      return card;
    }

    function loadComments(page = 1, append = false) {
      if (!listUrl) return;
      fetch(`${listUrl}?page=${page}&page_size=10`)
        .then(res => res.json())
        .then(res => {
          if (loadingIndicator) loadingIndicator.style.display = 'none';
          if (res.status === 'success') {
            currentPage = res.current_page;
            totalPages = res.total_pages;
            if (countBadge) countBadge.textContent = `${res.total_count} 条评论`;

            if (!append) itemsList.innerHTML = '';

            if (res.items.length === 0 && !append) {
              itemsList.innerHTML = '<div class="text-center py-4 text-muted small bg-white rounded border">暂无评论，欢迎发表第一条观点！</div>';
            } else {
              res.items.forEach(c => {
                const card = renderCommentCard(c, false);
                itemsList.appendChild(card);
                if (c.reply_count > 0) {
                  loadReplies(c.id);
                }
              });
            }

            if (currentPage < totalPages && paginationArea) {
              paginationArea.style.display = 'block';
            } else if (paginationArea) {
              paginationArea.style.display = 'none';
            }
          }
        })
        .catch(err => {
          if (loadingIndicator) loadingIndicator.textContent = '评论加载失败，请刷新页面重试。';
        });
    }

    function loadReplies(rootId) {
      const container = document.getElementById(`replies-stream-${rootId}`);
      if (!container) return;
      fetch(`/api/v1/comments/comments/${rootId}/replies/`)
        .then(res => res.json())
        .then(res => {
          if (res.status === 'success' && res.items) {
            container.innerHTML = '';
            res.items.forEach(r => {
              container.appendChild(renderCommentCard(r, true));
            });
          }
        });
    }

    // 1. 表单提交
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      const submitBtn = document.getElementById('comment-submit-btn');
      if (submitBtn) submitBtn.disabled = true;

      const formData = new FormData(form);

      fetch(postUrl, {
        method: 'POST',
        body: formData,
      })
        .then(res => res.json())
        .then(data => {
          if (submitBtn) submitBtn.disabled = false;
          if (data.status === 'success') {
            showAlert('评论发表成功！' + (data.data && data.data.is_publicly_visible ? '' : '已提交风控系统，通过后将公开显示。'), true);
            form.querySelector('textarea[name="content"]').value = '';
            resetReplyTarget();
            loadComments(1, false);
          } else {
            showAlert(data.message || '评论提交失败，请稍后重试。', false);
          }
        })
        .catch(err => {
          if (submitBtn) submitBtn.disabled = false;
          showAlert('网络异常，提交失败。', false);
        });
    });

    // 2. 回复事件委托
    document.addEventListener('click', function (e) {
      const replyBtn = e.target.closest('.reply-trigger-btn');
      if (replyBtn) {
        e.preventDefault();
        const cid = replyBtn.getAttribute('data-id');
        const author = replyBtn.getAttribute('data-author');
        if (parentIdInput) parentIdInput.value = cid;
        if (replyToIdInput) replyToIdInput.value = cid;
        if (replyTargetName) replyTargetName.textContent = author;
        if (replyNotice) replyNotice.style.display = 'flex';
        form.scrollIntoView({ behavior: 'smooth' });
        const textarea = form.querySelector('textarea[name="content"]');
        if (textarea) textarea.focus();
      }

      // 点赞事件委托
      const reactBtn = e.target.closest('.react-btn');
      if (reactBtn) {
        e.preventDefault();
        const cid = reactBtn.getAttribute('data-id');
        const action = reactBtn.getAttribute('data-action');
        const formData = new FormData();
        formData.append('reaction_type', action);
        fetch(`/api/v1/comments/comments/${cid}/react/`, {
          method: 'POST',
          body: formData,
        })
          .then(res => res.json())
          .then(res => {
            if (res.status === 'success' && res.data) {
              const likeSpan = reactBtn.querySelector('.like-num');
              if (likeSpan) likeSpan.textContent = res.data.like_count;
            }
          });
      }
    });

    function resetReplyTarget() {
      if (parentIdInput) parentIdInput.value = '';
      if (replyToIdInput) replyToIdInput.value = '';
      if (replyNotice) replyNotice.style.display = 'none';
    }

    if (cancelReplyBtn) {
      cancelReplyBtn.addEventListener('click', resetReplyTarget);
    }

    if (loadMoreBtn) {
      loadMoreBtn.addEventListener('click', function () {
        if (currentPage < totalPages) {
          loadComments(currentPage + 1, true);
        }
      });
    }

    // 初始化加载首屏评论
    loadComments(1, false);
  });
})();
