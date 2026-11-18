/**
 * 全站通用评论互动组件前端控制器 (CommentsWidget)
 * 落实 决策三-A: 轻量 AJAX 提交 + 友好免刷新回显 + 两层扁平回复 + 点赞互动
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    const section = document.getElementById('comments-section');
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
    const requestKeyInput = document.getElementById('comment-request-key-input');
    const captchaWrapper = document.getElementById('comment-captcha-wrapper');
    const loadMoreBtn = document.getElementById('load-more-comments-btn');
    const paginationArea = document.getElementById('comments-pagination-area');

    if (!section || !itemsList) {
      return;
    }

    const isPreview = section.getAttribute('data-is-preview') === 'true';
    if (isPreview) {
      return;
    }

    const listUrl = section.getAttribute('data-list-url') || (form && form.getAttribute('data-list-url'));
    const postUrl = section.getAttribute('data-post-url') || (form && form.getAttribute('data-post-url'));
    const repliesUrlTpl = section.getAttribute('data-replies-url-tpl') || '/api/v1/comments/0/replies/';
    const editUrlTpl = section.getAttribute('data-edit-url-tpl') || '/api/v1/comments/0/edit/';
    const reactUrlTpl = section.getAttribute('data-react-url-tpl') || '/api/v1/comments/0/react/';
    const deleteUrlTpl = section.getAttribute('data-delete-url-tpl') || '/api/v1/comments/0/delete/';
    let allowComments = section.getAttribute('data-allow-comments') !== 'false';
    let requiresCaptcha = section.getAttribute('data-requires-captcha') === 'true';

    let currentPage = 1;
    let totalPages = 1;
    let currentSort = 'latest';

    function generateRequestKey() {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID().replace(/-/g, '');
      }
      return 'req_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);
    }

    function activateCaptchaWidget(scope) {
      if (!captchaWrapper) return;
      requiresCaptcha = true;
      captchaWrapper.hidden = false;
      const widget = captchaWrapper.querySelector('.js-captcha-widget');
      if (!widget) return;
      if (scope) {
        widget.setAttribute('data-captcha-scope', scope);
      }
      widget.setAttribute('data-defer-load', 'false');
      const codeInput = widget.querySelector('input[name="captcha_code"]');
      if (codeInput) {
        codeInput.required = true;
      }
      if (window.MyblogCaptcha && typeof window.MyblogCaptcha.refreshWidget === 'function') {
        window.MyblogCaptcha.refreshWidget(widget);
      }
    }

    if (captchaWrapper && requiresCaptcha) {
      const codeInput = captchaWrapper.querySelector('input[name="captcha_code"]');
      if (codeInput) codeInput.required = true;
    }

    function buildActionUrl(tpl, id) {
      return tpl.replace('/0/', `/${id}/`);
    }

    function getCookie(name) {
      if (!document.cookie) return '';
      const cookies = document.cookie.split(';');
      for (let i = 0; i < cookies.length; i++) {
        const cookie = cookies[i].trim();
        if (cookie.substring(0, name.length + 1) === (name + '=')) {
          return decodeURIComponent(cookie.substring(name.length + 1));
        }
      }
      return '';
    }

    function getCsrfToken() {
      const input = section.querySelector('input[name="csrfmiddlewaretoken"]');
      if (input && input.value) return input.value;
      return getCookie('csrftoken');
    }

    function parseJsonResponse(res) {
      return res.text().then(text => {
        let data = null;
        try {
          data = text ? JSON.parse(text) : {};
        } catch (_err) {
          data = null;
        }
        return { ok: res.ok, status: res.status, data };
      });
    }

    function showAlert(msg, isSuccess = true) {
      if (!formAlert) return;
      formAlert.className = `comment-alert ${isSuccess ? 'is-success' : 'is-error'}`;
      formAlert.textContent = msg;
      formAlert.style.display = 'block';
      clearTimeout(showAlert._timer);
      showAlert._timer = setTimeout(() => {
        formAlert.style.display = 'none';
      }, 5000);
    }

    function showCardFeedback(targetBtn, msg, isSuccess = false) {
      const card = targetBtn ? targetBtn.closest('.comment-card') : null;
      if (!card) return;
      let box = card.querySelector('.comment-card-inline-alert');
      if (!box) {
        box = document.createElement('div');
        box.setAttribute('role', 'alert');
        const footer = card.querySelector('.comment-card-footer');
        if (footer && footer.parentNode) {
          footer.insertAdjacentElement('afterend', box);
        } else {
          card.appendChild(box);
        }
      }
      box.className = `comment-card-inline-alert ${isSuccess ? 'is-success' : 'is-error'}`;
      box.textContent = msg;
      box.style.display = 'block';
      clearTimeout(box._timer);
      box._timer = setTimeout(() => {
        box.style.display = 'none';
      }, 5000);
    }

    function renderCommentCard(c, isReply = false, rootId = null) {
      const effectiveRootId = isReply ? (rootId || c.root_id || c.id) : c.id;
      const card = document.createElement('div');
      card.id = `comment-${c.id}`;

      if (c.kind === 'author_deleted') {
        card.className = 'comment-card comment-card-deleted';
        const header = document.createElement('div');
        header.className = 'comment-card-header';
        const msgSpan = document.createElement('span');
        msgSpan.textContent = c.message || '该评论已由作者自行删除';
        const dateSpan = document.createElement('span');
        dateSpan.className = 'comment-date';
        dateSpan.textContent = c.created_at || '';
        header.appendChild(msgSpan);
        header.appendChild(dateSpan);
        card.appendChild(header);

        const repliesBox = document.createElement('div');
        repliesBox.className = 'replies-stream-container';
        repliesBox.id = `replies-stream-${c.id}`;
        card.appendChild(repliesBox);
        return card;
      }

      card.className = `comment-card ${isReply ? 'is-reply' : ''}`;

      const authorName = (c.author && (c.author.display_name || c.author.username)) ? (c.author.display_name || c.author.username) : '匿名读者';
      const dateStr = c.created_at || '';

      const header = document.createElement('div');
      header.className = 'comment-card-header';

      const metaLeft = document.createElement('div');
      metaLeft.className = 'comment-meta-left';

      const authorEl = document.createElement('span');
      authorEl.className = 'comment-author';
      authorEl.textContent = authorName;
      metaLeft.appendChild(authorEl);

      if (!isReply && c.is_pinned) {
        const pinBadge = document.createElement('span');
        pinBadge.className = 'comment-pin-badge';
        pinBadge.textContent = '📌 置顶';
        metaLeft.appendChild(pinBadge);
      }

      if (isReply && c.replied_to_user && (c.replied_to_user.display_name || c.replied_to_user.username)) {
        const targetEl = document.createElement('span');
        targetEl.className = 'comment-reply-target';
        targetEl.textContent = `回复 @${c.replied_to_user.display_name || c.replied_to_user.username}`;
        metaLeft.appendChild(targetEl);
      }

      const dateEl = document.createElement('span');
      dateEl.className = 'comment-date';
      dateEl.textContent = dateStr;
      metaLeft.appendChild(dateEl);

      const actionsTop = document.createElement('div');
      actionsTop.className = 'comment-actions-top';

      if (allowComments && form) {
        const replyBtn = document.createElement('button');
        replyBtn.type = 'button';
        replyBtn.className = 'comment-text-btn reply-trigger-btn';
        replyBtn.setAttribute('data-id', String(c.id));
        replyBtn.setAttribute('data-root-id', String(effectiveRootId));
        replyBtn.setAttribute('data-author', authorName);
        replyBtn.textContent = '回复';
        actionsTop.appendChild(replyBtn);
      }

      if (allowComments && c.is_editable) {
        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'comment-text-btn edit-comment-btn';
        editBtn.setAttribute('data-id', String(c.id));
        editBtn.setAttribute('data-version-id', String(c.latest_version_id || ''));
        editBtn.setAttribute('data-generation', String(c.state_generation || ''));
        editBtn.setAttribute('data-raw-content', c.editable_raw_content || '');
        editBtn.textContent = '编辑';
        actionsTop.appendChild(editBtn);
      }

      if (c.can_delete) {
        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'comment-text-btn is-danger delete-comment-btn';
        delBtn.setAttribute('data-id', String(c.id));
        delBtn.textContent = '删除';
        actionsTop.appendChild(delBtn);
      }

      header.appendChild(metaLeft);
      header.appendChild(actionsTop);
      card.appendChild(header);

      const bodyEl = document.createElement('div');
      bodyEl.className = 'comment-body-html';
      bodyEl.innerHTML = c.safe_html || '';
      card.appendChild(bodyEl);

      if (c.has_pending_edit) {
        const pendingBanner = document.createElement('div');
        pendingBanner.className = 'comment-pending-edit-note';
        pendingBanner.textContent = '您提交的修改版本正在等待审核，审核通过前公网仍展示原已批准版本。';
        card.appendChild(pendingBanner);
      }

      const footer = document.createElement('div');
      footer.className = 'comment-card-footer';

      const likeBtn = document.createElement('button');
      likeBtn.type = 'button';
      const isLiked = c.user_reaction === 1;
      likeBtn.className = `comment-react-btn react-btn ${isLiked ? 'is-reacted' : ''}`;
      likeBtn.setAttribute('data-id', String(c.id));
      likeBtn.setAttribute('data-liked', isLiked ? '1' : '0');
      if (!allowComments && !isLiked) {
        likeBtn.disabled = true;
      }
      const iconText = document.createTextNode('👍 ');
      const numSpan = document.createElement('span');
      numSpan.className = 'like-num';
      numSpan.textContent = String(c.like_count || 0);
      likeBtn.appendChild(iconText);
      likeBtn.appendChild(numSpan);
      footer.appendChild(likeBtn);
      card.appendChild(footer);

      if (!isReply) {
        const repliesBox = document.createElement('div');
        repliesBox.className = 'replies-stream-container';
        repliesBox.id = `replies-stream-${c.id}`;
        card.appendChild(repliesBox);
      }
      return card;
    }

    function loadComments(page = 1, append = false) {
      if (!listUrl) return;
      fetch(`${listUrl}?page=${page}&page_size=10&sort=${encodeURIComponent(currentSort)}`, {
        credentials: 'same-origin',
      })
        .then(parseJsonResponse)
        .then(({ ok, status, data }) => {
          if (loadingIndicator) loadingIndicator.style.display = 'none';
          if (!ok || !data || data.status !== 'success') {
            if (status === 403) {
              if (countBadge) countBadge.textContent = '暂不可见';
              itemsList.innerHTML = '';
              const notice = document.createElement('div');
              notice.className = 'comments-empty-state';
              notice.textContent = (data && data.message) || '当前页面暂不开放评论查看。';
              itemsList.appendChild(notice);
              return;
            }
            if (loadingIndicator) {
              loadingIndicator.style.display = 'block';
              loadingIndicator.textContent = (data && data.message) || '评论加载失败，请刷新页面重试。';
            }
            if (countBadge) countBadge.textContent = '加载异常';
            return;
          }

          if (data.comment_settings) {
            allowComments = Boolean(data.comment_settings.allow_comments);
            if (data.comment_settings.requires_captcha && form) {
              const widget = captchaWrapper ? captchaWrapper.querySelector('.js-captcha-widget') : null;
              const hasLoaded = widget && widget.querySelector('input[name="captcha_id"]')?.value;
              if (!hasLoaded) {
                activateCaptchaWidget(data.comment_settings.captcha_scope || '');
              }
            }
          }

          currentPage = data.current_page;
          totalPages = data.total_pages;
          if (countBadge) countBadge.textContent = `${data.total_count} 条评论`;

          if (!append) itemsList.innerHTML = '';

          if (data.items.length === 0 && !append) {
            const emptyEl = document.createElement('div');
            emptyEl.className = 'comments-empty-state';
            emptyEl.textContent = allowComments ? '暂无评论，欢迎发表第一条观点！' : '暂无公开评论。';
            itemsList.appendChild(emptyEl);
          } else {
            data.items.forEach(c => {
              const card = renderCommentCard(c, false, c.id);
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
        })
        .catch(() => {
          if (loadingIndicator) {
            loadingIndicator.style.display = 'block';
            loadingIndicator.textContent = '评论加载失败，请刷新页面重试。';
          }
          if (countBadge) countBadge.textContent = '加载失败';
        });
    }

    function loadReplies(rootId) {
      const container = document.getElementById(`replies-stream-${rootId}`);
      if (!container) return;
      fetch(buildActionUrl(repliesUrlTpl, rootId), { credentials: 'same-origin' })
        .then(parseJsonResponse)
        .then(({ ok, data }) => {
          if (ok && data && data.status === 'success' && data.items) {
            container.innerHTML = '';
            data.items.forEach(r => {
              container.appendChild(renderCommentCard(r, true, rootId));
            });
          }
        });
    }

    // 1. 表单提交
    if (form) {
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        if (!postUrl) return;
        if (requestKeyInput && !requestKeyInput.value) {
          requestKeyInput.value = generateRequestKey();
        }
        const submitBtn = document.getElementById('comment-submit-btn');
        if (submitBtn) submitBtn.disabled = true;

        const formData = new FormData(form);

        fetch(postUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'X-CSRFToken': getCsrfToken(),
          },
          body: formData,
        })
          .then(parseJsonResponse)
          .then(({ ok, data }) => {
            if (submitBtn) submitBtn.disabled = false;
            if (ok && data && data.status === 'success') {
              showAlert('评论发表成功！' + (data.data && data.data.is_publicly_visible ? '' : '已提交审核，通过后将公开显示。'), true);
              const textarea = form.querySelector('textarea[name="content"]');
              if (textarea) textarea.value = '';
              if (requestKeyInput) requestKeyInput.value = '';
              resetReplyTarget();
              if (requiresCaptcha && captchaWrapper) {
                const widget = captchaWrapper.querySelector('.js-captcha-widget');
                if (widget && window.MyblogCaptcha) {
                  window.MyblogCaptcha.refreshWidget(widget);
                }
              }
              loadComments(1, false);
            } else {
              const errCode = data && data.code;
              if (errCode === 'captcha_required' || errCode === 'captcha_invalid' || errCode === 'captcha_binding_missing') {
                activateCaptchaWidget((data && data.captcha_scope) || '');
              } else if (requiresCaptcha && captchaWrapper) {
                const widget = captchaWrapper.querySelector('.js-captcha-widget');
                if (widget && window.MyblogCaptcha) {
                  window.MyblogCaptcha.refreshWidget(widget);
                }
              }
              if (requestKeyInput && errCode === 'idempotency_conflict') {
                requestKeyInput.value = '';
              }
              showAlert((data && data.message) || '评论提交失败，请稍后重试。', false);
            }
          })
          .catch(() => {
            if (submitBtn) submitBtn.disabled = false;
            showAlert('网络异常，提交失败。', false);
          });
      });

      const textareaEl = form.querySelector('textarea[name="content"]');
      if (textareaEl) {
        textareaEl.addEventListener('input', function () {
          if (requestKeyInput) requestKeyInput.value = '';
        });
      }
    }

    // 2. 排序、回复、点赞与删除事件委托
    section.addEventListener('click', function (e) {
      const sortBtn = e.target.closest('.comment-sort-btn');
      if (sortBtn) {
        const nextSort = sortBtn.getAttribute('data-sort') || 'latest';
        if (nextSort !== currentSort) {
          currentSort = nextSort;
          section.querySelectorAll('.comment-sort-btn').forEach(btn => btn.classList.remove('is-active'));
          sortBtn.classList.add('is-active');
          loadComments(1, false);
        }
        return;
      }

      const replyBtn = e.target.closest('.reply-trigger-btn');
      if (replyBtn && form) {
        e.preventDefault();
        const cid = replyBtn.getAttribute('data-id');
        const rootId = replyBtn.getAttribute('data-root-id') || cid;
        const author = replyBtn.getAttribute('data-author') || '';
        if (parentIdInput) parentIdInput.value = rootId;
        if (replyToIdInput) replyToIdInput.value = cid;
        if (replyTargetName) replyTargetName.textContent = author;
        if (replyNotice) replyNotice.style.display = 'flex';
        form.scrollIntoView({ behavior: 'smooth' });
        const textarea = form.querySelector('textarea[name="content"]');
        if (textarea) textarea.focus();
        return;
      }

      // 点赞事件委托
      const reactBtn = e.target.closest('.react-btn');
      if (reactBtn) {
        e.preventDefault();
        const cid = reactBtn.getAttribute('data-id');
        const wasLiked = reactBtn.getAttribute('data-liked') === '1';
        const nextReaction = wasLiked ? '0' : '1';
        const formData = new FormData();
        formData.append('reaction', nextReaction);
        fetch(buildActionUrl(reactUrlTpl, cid), {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'X-CSRFToken': getCsrfToken(),
          },
          body: formData,
        })
          .then(parseJsonResponse)
          .then(({ ok, data }) => {
            if (ok && data && data.status === 'success' && data.data) {
              const likeSpan = reactBtn.querySelector('.like-num');
              if (likeSpan) likeSpan.textContent = data.data.like_count;
              const nowLiked = nextReaction === '1';
              reactBtn.setAttribute('data-liked', nowLiked ? '1' : '0');
              reactBtn.classList.toggle('is-reacted', nowLiked);
            } else {
              const errMsg = (data && data.message) || '操作失败，请先登录后重试。';
              showCardFeedback(reactBtn, errMsg, false);
              showAlert(errMsg, false);
            }
          })
          .catch(() => {
            const errMsg = '网络异常，互动操作失败，请稍后重试。';
            showCardFeedback(reactBtn, errMsg, false);
            showAlert(errMsg, false);
          });
        return;
      }

      // 作者自删事件委托
      const delBtn = e.target.closest('.delete-comment-btn');
      if (delBtn) {
        e.preventDefault();
        if (!window.confirm('确定删除这条评论吗？')) {
          return;
        }
        const cid = delBtn.getAttribute('data-id');
        fetch(buildActionUrl(deleteUrlTpl, cid), {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'X-CSRFToken': getCsrfToken(),
          },
        })
          .then(parseJsonResponse)
          .then(({ ok, data }) => {
            if (ok && data && data.status === 'success') {
              showAlert('评论已删除。', true);
              loadComments(1, false);
            } else {
              showAlert((data && data.message) || '删除失败，请稍后重试。', false);
            }
          });
        return;
      }

      // 5 分钟安全编辑窗口：展开内联编辑器并提交修改
      const editBtn = e.target.closest('.edit-comment-btn');
      if (editBtn) {
        e.preventDefault();
        const card = editBtn.closest('.comment-card');
        if (!card) return;
        const existingEditor = card.querySelector('.comment-inline-edit-form');
        if (existingEditor) {
          existingEditor.remove();
          return;
        }
        const cid = editBtn.getAttribute('data-id');
        const verId = editBtn.getAttribute('data-version-id') || '';
        const gen = editBtn.getAttribute('data-generation') || '';
        const rawContent = editBtn.getAttribute('data-raw-content') || '';

        const editForm = document.createElement('form');
        editForm.className = 'comment-inline-edit-form';
        const ta = document.createElement('textarea');
        ta.className = 'comment-textarea comment-inline-edit-textarea';
        ta.rows = 3;
        ta.required = true;
        ta.maxLength = 2000;
        ta.value = rawContent;
        const btnRow = document.createElement('div');
        btnRow.className = 'comment-inline-edit-actions';
        const saveBtn = document.createElement('button');
        saveBtn.type = 'submit';
        saveBtn.className = 'comment-submit-btn';
        saveBtn.textContent = '保存修改';
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'comment-text-btn cancel-inline-edit-btn';
        cancelBtn.textContent = '取消';
        cancelBtn.addEventListener('click', () => editForm.remove());
        btnRow.appendChild(saveBtn);
        btnRow.appendChild(cancelBtn);
        editForm.appendChild(ta);
        editForm.appendChild(btnRow);

        editForm.addEventListener('submit', function (ev) {
          ev.preventDefault();
          const nextText = (ta.value || '').trim();
          if (!nextText) {
            showCardFeedback(editBtn, '修改后的评论内容不能为空。', false);
            return;
          }
          saveBtn.disabled = true;
          const fd = new FormData();
          fd.append('content', nextText);
          if (verId) fd.append('expected_version_id', verId);
          if (gen) fd.append('expected_generation', gen);
          fetch(buildActionUrl(editUrlTpl, cid), {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': getCsrfToken() },
            body: fd,
          })
            .then(parseJsonResponse)
            .then(({ ok, data }) => {
              saveBtn.disabled = false;
              if (ok && data && data.status === 'success') {
                const msg = (data.data && data.data.status === 'approved')
                  ? '评论修改已生效。'
                  : '修改已提交审核，审核通过前公网仍展示原版本。';
                showAlert(msg, true);
                editForm.remove();
                loadComments(currentPage, false);
              } else {
                showCardFeedback(editBtn, (data && data.message) || '修改失败，请刷新后重试。', false);
              }
            })
            .catch(() => {
              saveBtn.disabled = false;
              showCardFeedback(editBtn, '网络异常，修改提交失败。', false);
            });
        });

        const footer = card.querySelector('.comment-card-footer');
        if (footer) {
          footer.insertAdjacentElement('afterend', editForm);
        } else {
          card.appendChild(editForm);
        }
        ta.focus();
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
