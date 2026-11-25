/**
 * 全站通用评论互动组件前端控制器 (CommentsWidget)
 * 落实：Markdown 工具栏 + Unicode Emoji 表情面板 + 实时安全预览 + 两层扁平回复 + 赞踩互动 + 公共渲染引擎联动
 */
(function () {
  'use strict';

  const EMOJI_GROUPS = [
    {
      title: '常用表情',
      items: ['😀', '😄', '😅', '😂', '😊', '🤔', '😍', '🤩', '😎', '😭', '😡', '🙏'],
    },
    {
      title: '互动赞赏',
      items: ['👍', '👎', '👏', '🔥', '🎉', '❤️', '💯', '✨', '🚀', '💡', '⚡', '🏆'],
    },
    {
      title: '技术符号',
      items: ['💻', '🐛', '✅', '❌', '⚠️', '📌', '🔍', '📝', '📊', '🔗', '🎯', '🛠️'],
    },
  ];

  function renderRichContent(rootEl) {
    if (!rootEl) return;
    if (typeof window.enhanceCodeBlocks === 'function') {
      window.enhanceCodeBlocks(rootEl);
    }
    if (typeof window.renderContentMath === 'function') {
      window.renderContentMath(rootEl);
    }
    if (typeof window.initMermaidBlocks === 'function') {
      window.initMermaidBlocks(rootEl);
    }
  }

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
    const previewUrl = section.getAttribute('data-preview-url') || '';
    const repliesUrlTpl = section.getAttribute('data-replies-url-tpl') || '/api/v1/comments/0/replies/';
    const editUrlTpl = section.getAttribute('data-edit-url-tpl') || '/api/v1/comments/0/edit/';
    const reactUrlTpl = section.getAttribute('data-react-url-tpl') || '/api/v1/comments/0/react/';
    const deleteUrlTpl = section.getAttribute('data-delete-url-tpl') || '/api/v1/comments/0/delete/';
    let allowComments = section.getAttribute('data-allow-comments') !== 'false';
    let requiresCaptcha = section.getAttribute('data-requires-captcha') === 'true';
    let maxChars = parseInt(section.getAttribute('data-max-chars') || '3000', 10) || 3000;
    let allowRichEmbeds = section.getAttribute('data-allow-rich-embeds') === 'true';
    let activePreviewUrl = previewUrl;

    let currentPage = 1;
    let totalPages = 1;
    let currentSort = 'latest';
    let mainEditorApi = null;

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

    function insertAroundSelection(textarea, prefix, suffix, placeholder) {
      const val = textarea.value || '';
      const start = typeof textarea.selectionStart === 'number' ? textarea.selectionStart : val.length;
      const end = typeof textarea.selectionEnd === 'number' ? textarea.selectionEnd : val.length;
      const selected = val.slice(start, end) || placeholder || '';
      const inserted = prefix + selected + suffix;
      textarea.value = val.slice(0, start) + inserted + val.slice(end);
      const cursorStart = start + prefix.length;
      const cursorEnd = cursorStart + selected.length;
      textarea.focus();
      if (typeof textarea.setSelectionRange === 'function') {
        textarea.setSelectionRange(cursorStart, cursorEnd);
      }
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function insertBlockSnippet(textarea, snippet) {
      const val = textarea.value || '';
      const start = typeof textarea.selectionStart === 'number' ? textarea.selectionStart : val.length;
      const end = typeof textarea.selectionEnd === 'number' ? textarea.selectionEnd : val.length;
      const before = val.slice(0, start);
      const after = val.slice(end);
      const needLeadNl = before && !before.endsWith('\n') ? '\n\n' : (before && !before.endsWith('\n\n') ? '\n' : '');
      const needTrailNl = after && !after.startsWith('\n') ? '\n\n' : '';
      const inserted = needLeadNl + snippet + needTrailNl;
      textarea.value = before + inserted + after;
      const caret = (before + inserted).length;
      textarea.focus();
      if (typeof textarea.setSelectionRange === 'function') {
        textarea.setSelectionRange(caret, caret);
      }
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function createCommentEditorUI(options) {
      const textarea = options.textarea;
      if (!textarea) return null;

      let currentMaxChars = options.maxChars || maxChars;
      let canRich = Boolean(options.allowRichEmbeds);
      let currentPreviewUrl = options.previewUrl || activePreviewUrl;
      let currentMode = 'write';
      let previewSeq = 0;
      let previewTimer = null;

      const shell = document.createElement('div');
      shell.className = 'comment-editor-shell';

      const toolbar = document.createElement('div');
      toolbar.className = 'comment-editor-toolbar';

      const tools = document.createElement('div');
      tools.className = 'comment-editor-tools';
      tools.setAttribute('role', 'toolbar');
      tools.setAttribute('aria-label', '评论 Markdown 格式工具栏');

      function makeToolBtn(label, title, onClick) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'comment-tool-btn';
        btn.textContent = label;
        btn.title = title;
        btn.setAttribute('aria-label', title);
        btn.addEventListener('click', function (ev) {
          ev.preventDefault();
          onClick();
        });
        return btn;
      }

      function makeSep() {
        const sep = document.createElement('span');
        sep.className = 'comment-tool-sep';
        sep.setAttribute('aria-hidden', 'true');
        return sep;
      }

      tools.appendChild(makeToolBtn('B', '加粗 (**文本**)', () => insertAroundSelection(textarea, '**', '**', '加粗文本')));
      tools.appendChild(makeToolBtn('I', '斜体 (*文本*)', () => insertAroundSelection(textarea, '*', '*', '斜体文本')));
      tools.appendChild(makeToolBtn('❝', '引用 (> 文本)', () => insertBlockSnippet(textarea, '> 引用内容')));
      tools.appendChild(makeToolBtn('•', '列表 (- 列表项)', () => insertBlockSnippet(textarea, '- 列表项 1\n- 列表项 2')));
      tools.appendChild(makeSep());
      tools.appendChild(makeToolBtn('`', '行内代码 (`code`)', () => insertAroundSelection(textarea, '`', '`', 'code')));
      tools.appendChild(makeToolBtn('{ }', '代码块 (```lang)', () => insertBlockSnippet(textarea, '```python\nprint("Hello, myblog!")\n```')));
      tools.appendChild(makeToolBtn('田', '插入表格', () => insertBlockSnippet(textarea, '| 列 1 | 列 2 |\n| --- | --- |\n| 内容 A | 内容 B |')));

      const richBtns = [];
      if (canRich) {
        const sepRich = makeSep();
        const inlineMathBtn = makeToolBtn('∑', '行内公式 ($...$)', () => insertAroundSelection(textarea, '$', '$', 'E = mc^2'));
        const blockMathBtn = makeToolBtn('∬', '块级公式 ($$...$$)', () => insertBlockSnippet(textarea, '$$\n\\int_0^1 x^2\\,dx = \\frac{1}{3}\n$$'));
        const mermaidBtn = makeToolBtn('◈', '插入 Mermaid 流程图', () => insertBlockSnippet(textarea, '```mermaid\nflowchart LR\n  A[问题] --> B[方案]\n  B --> C[验证]\n```'));
        richBtns.push(sepRich, inlineMathBtn, blockMathBtn, mermaidBtn);
        tools.appendChild(sepRich);
        tools.appendChild(inlineMathBtn);
        tools.appendChild(blockMathBtn);
        tools.appendChild(mermaidBtn);
      }

      tools.appendChild(makeSep());

      // Emoji 表情包选择弹出框
      const emojiWrap = document.createElement('div');
      emojiWrap.className = 'comment-emoji-wrap';
      const emojiTrigger = document.createElement('button');
      emojiTrigger.type = 'button';
      emojiTrigger.className = 'comment-tool-btn comment-emoji-trigger';
      emojiTrigger.textContent = '😊';
      emojiTrigger.title = '插入 Emoji 表情';
      emojiTrigger.setAttribute('aria-label', '插入 Emoji 表情');
      emojiTrigger.setAttribute('aria-expanded', 'false');
      emojiTrigger.setAttribute('aria-haspopup', 'true');

      const emojiPopover = document.createElement('div');
      emojiPopover.className = 'comment-emoji-popover';
      emojiPopover.hidden = true;
      emojiPopover.setAttribute('role', 'dialog');
      emojiPopover.setAttribute('aria-label', '选择表情');

      EMOJI_GROUPS.forEach(group => {
        const titleEl = document.createElement('div');
        titleEl.className = 'comment-emoji-group-title';
        titleEl.textContent = group.title;
        emojiPopover.appendChild(titleEl);

        const grid = document.createElement('div');
        grid.className = 'comment-emoji-grid';
        group.items.forEach(ch => {
          const eBtn = document.createElement('button');
          eBtn.type = 'button';
          eBtn.className = 'comment-emoji-item';
          eBtn.textContent = ch;
          eBtn.setAttribute('aria-label', `插入表情 ${ch}`);
          eBtn.addEventListener('click', function (ev) {
            ev.preventDefault();
            insertAroundSelection(textarea, ch, '', '');
            closeEmojiPopover();
          });
          grid.appendChild(eBtn);
        });
        emojiPopover.appendChild(grid);
      });

      function closeEmojiPopover() {
        emojiPopover.hidden = true;
        emojiTrigger.setAttribute('aria-expanded', 'false');
        emojiTrigger.classList.remove('is-active');
      }

      emojiTrigger.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        const nextOpen = emojiPopover.hidden;
        emojiPopover.hidden = !nextOpen;
        emojiTrigger.setAttribute('aria-expanded', nextOpen ? 'true' : 'false');
        emojiTrigger.classList.toggle('is-active', nextOpen);
      });

      document.addEventListener('click', function (ev) {
        if (!emojiPopover.hidden && !emojiWrap.contains(ev.target)) {
          closeEmojiPopover();
        }
      });

      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && !emojiPopover.hidden) {
          closeEmojiPopover();
          emojiTrigger.focus();
        }
      });

      emojiWrap.appendChild(emojiTrigger);
      emojiWrap.appendChild(emojiPopover);
      tools.appendChild(emojiWrap);

      // 模式切换（编辑 / 预览 / 分栏）
      const modes = document.createElement('div');
      modes.className = 'comment-editor-modes';
      modes.setAttribute('role', 'group');
      modes.setAttribute('aria-label', '编辑预览模式');

      const modeBtns = {};
      [
        { id: 'write', label: '编辑' },
        { id: 'preview', label: '预览' },
        { id: 'split', label: '分栏' },
      ].forEach(m => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = `comment-mode-btn${m.id === 'write' ? ' is-active' : ''}`;
        b.setAttribute('data-mode', m.id);
        b.textContent = m.label;
        b.addEventListener('click', function () {
          setMode(m.id);
        });
        modeBtns[m.id] = b;
        modes.appendChild(b);
      });

      toolbar.appendChild(tools);
      toolbar.appendChild(modes);

      const workspace = document.createElement('div');
      workspace.className = 'comment-editor-workspace';

      const previewPane = document.createElement('div');
      previewPane.className = 'comment-editor-preview-pane';
      previewPane.hidden = true;

      const previewSurface = document.createElement('div');
      previewSurface.className = 'comment-body-html comment-preview-surface';
      previewSurface.setAttribute('data-code-highlight', '');
      previewPane.appendChild(previewSurface);

      const statusBar = document.createElement('div');
      statusBar.className = 'comment-editor-statusbar';
      const tipSpan = document.createElement('span');
      tipSpan.textContent = canRich ? '支持 Markdown · Emoji · 代码 · 公式 · Mermaid' : '支持基础 Markdown · Emoji · 代码块';
      const counterSpan = document.createElement('span');
      counterSpan.className = 'comment-char-counter';
      statusBar.appendChild(tipSpan);
      statusBar.appendChild(counterSpan);

      // 挂载到原 textarea 所在位置
      const parent = textarea.parentNode;
      parent.insertBefore(shell, textarea);
      workspace.appendChild(textarea);
      workspace.appendChild(previewPane);
      shell.appendChild(toolbar);
      shell.appendChild(workspace);
      shell.appendChild(statusBar);

      function updateCounter() {
        const len = (textarea.value || '').length;
        counterSpan.textContent = `${len} / ${currentMaxChars}`;
        counterSpan.classList.toggle('is-near-limit', len >= Math.floor(currentMaxChars * 0.9) && len <= currentMaxChars);
        counterSpan.classList.toggle('is-over-limit', len > currentMaxChars);
      }

      function renderPreview() {
        const raw = textarea.value || '';
        if (!raw.trim()) {
          previewSurface.innerHTML = '';
          const emptyP = document.createElement('p');
          emptyP.className = 'comment-preview-empty';
          emptyP.textContent = '请输入内容后查看实时渲染效果。';
          previewSurface.appendChild(emptyP);
          return;
        }
        if (!currentPreviewUrl) {
          return;
        }
        const seq = ++previewSeq;
        const fd = new FormData();
        fd.append('content', raw);
        fetch(currentPreviewUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'X-CSRFToken': getCsrfToken() },
          body: fd,
        })
          .then(parseJsonResponse)
          .then(({ ok, data }) => {
            if (seq !== previewSeq) return;
            if (ok && data && data.status === 'success') {
              if (data.is_empty || !data.safe_html) {
                previewSurface.innerHTML = '';
                const p = document.createElement('p');
                p.className = 'comment-preview-empty';
                p.textContent = '暂无可预览内容。';
                previewSurface.appendChild(p);
              } else {
                previewSurface.innerHTML = data.safe_html;
                renderRichContent(previewSurface);
              }
            } else {
              previewSurface.innerHTML = '';
              const errP = document.createElement('p');
              errP.className = 'comment-preview-error';
              errP.textContent = (data && data.message) || '预览解析失败，请检查语法或字符限制。';
              previewSurface.appendChild(errP);
            }
          })
          .catch(() => {
            if (seq !== previewSeq) return;
            previewSurface.innerHTML = '';
            const errP = document.createElement('p');
            errP.className = 'comment-preview-error';
            errP.textContent = '网络异常，暂时无法预览。';
            previewSurface.appendChild(errP);
          });
      }

      function setMode(nextMode) {
        currentMode = nextMode;
        Object.keys(modeBtns).forEach(k => {
          modeBtns[k].classList.toggle('is-active', k === nextMode);
        });
        shell.classList.toggle('is-split-mode', nextMode === 'split');
        if (nextMode === 'write') {
          textarea.hidden = false;
          previewPane.hidden = true;
        } else if (nextMode === 'preview') {
          textarea.hidden = true;
          previewPane.hidden = false;
          renderPreview();
        } else {
          textarea.hidden = false;
          previewPane.hidden = false;
          renderPreview();
        }
      }

      textarea.addEventListener('input', function () {
        updateCounter();
        if (currentMode === 'split' || currentMode === 'preview') {
          clearTimeout(previewTimer);
          previewTimer = setTimeout(renderPreview, 420);
        }
      });

      updateCounter();
      return {
        setLimits(nextMaxChars, nextPreviewUrl) {
          if (nextMaxChars) {
            currentMaxChars = nextMaxChars;
            textarea.maxLength = nextMaxChars;
          }
          if (nextPreviewUrl) currentPreviewUrl = nextPreviewUrl;
          updateCounter();
        },
        reset() {
          updateCounter();
          if (currentMode !== 'write') {
            setMode('write');
          }
          previewSurface.innerHTML = '';
        },
        refreshCounter: updateCounter,
      };
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
      const authorKind = (c.author && c.author.kind) ? c.author.kind : '';
      const avatarUrl = (c.author && c.author.avatar_url) ? String(c.author.avatar_url) : '';
      const dateStr = c.created_at || '';

      const header = document.createElement('div');
      header.className = 'comment-card-header';

      const metaLeft = document.createElement('div');
      metaLeft.className = 'comment-meta-left';

      const fallbackAvatar = document.createElement('span');
      fallbackAvatar.className = 'comment-avatar-fallback';
      fallbackAvatar.setAttribute('aria-hidden', 'true');
      fallbackAvatar.textContent = (authorName || '匿').trim().charAt(0).toUpperCase();

      if (avatarUrl) {
        const avatarImg = document.createElement('img');
        avatarImg.className = 'comment-avatar';
        avatarImg.src = avatarUrl;
        avatarImg.alt = '';
        avatarImg.loading = 'lazy';
        avatarImg.addEventListener('error', function () {
          avatarImg.replaceWith(fallbackAvatar);
        });
        metaLeft.appendChild(avatarImg);
      } else {
        metaLeft.appendChild(fallbackAvatar);
      }

      const authorEl = document.createElement('span');
      authorEl.className = 'comment-author';
      authorEl.textContent = authorName;
      metaLeft.appendChild(authorEl);

      if (authorKind === 'guest') {
        const guestPill = document.createElement('span');
        guestPill.className = 'comment-role-pill comment-role-pill--guest';
        guestPill.textContent = '访客';
        metaLeft.appendChild(guestPill);
      } else if (authorKind === 'deleted_member') {
        const delPill = document.createElement('span');
        delPill.className = 'comment-role-pill';
        delPill.textContent = '已注销';
        metaLeft.appendChild(delPill);
      }

      if (!isReply && c.is_pinned) {
        const pinBadge = document.createElement('span');
        pinBadge.className = 'comment-pin-badge';
        pinBadge.textContent = '📌 置顶';
        metaLeft.appendChild(pinBadge);
      }

      const replyTargetLabel = (c.replied_to_user && (c.replied_to_user.display_name || c.replied_to_user.username))
        || c.replied_to_display_name
        || '';
      if (isReply && replyTargetLabel) {
        const targetEl = document.createElement('span');
        targetEl.className = 'comment-reply-target';
        targetEl.textContent = `回复 @${replyTargetLabel}`;
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
      bodyEl.setAttribute('data-code-highlight', '');
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

      const userReaction = Number(c.user_reaction || 0);
      const canReact = Boolean(c.can_react);

      function buildReactButton(targetVal, emoji, countClass, countVal, label) {
        const btn = document.createElement('button');
        btn.type = 'button';
        const active = userReaction === targetVal;
        btn.className = `comment-react-btn react-btn ${targetVal === 1 ? 'react-like-btn' : 'react-dislike-btn'}${active ? ' is-reacted' : ''}`;
        btn.setAttribute('data-id', String(c.id));
        btn.setAttribute('data-reaction', String(targetVal));
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
        btn.setAttribute('aria-label', label);
        if (!canReact && !active) {
          btn.disabled = true;
          btn.title = '本人评论或未登录状态下仅展示计数';
        }
        const emojiNode = document.createTextNode(`${emoji} `);
        const numSpan = document.createElement('span');
        numSpan.className = countClass;
        numSpan.textContent = String(countVal || 0);
        btn.appendChild(emojiNode);
        btn.appendChild(numSpan);
        return btn;
      }

      footer.appendChild(buildReactButton(1, '👍', 'like-num', c.like_count, '赞同这条评论'));
      footer.appendChild(buildReactButton(-1, '👎', 'dislike-num', c.dislike_count, '不赞同这条评论'));
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
            if (data.comment_settings.max_chars) {
              maxChars = parseInt(data.comment_settings.max_chars, 10) || maxChars;
            }
            if (typeof data.comment_settings.allow_rich_embeds === 'boolean') {
              allowRichEmbeds = data.comment_settings.allow_rich_embeds;
            }
            if (data.comment_settings.preview_url) {
              activePreviewUrl = data.comment_settings.preview_url;
            }
            if (mainEditorApi) {
              mainEditorApi.setLimits(maxChars, activePreviewUrl);
            }
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
            renderRichContent(itemsList);
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
            renderRichContent(container);
          }
        });
    }

    // 1. 表单提交
    if (form) {
      const mainTextarea = form.querySelector('textarea[name="content"]');
      if (mainTextarea) {
        mainEditorApi = createCommentEditorUI({
          textarea: mainTextarea,
          maxChars,
          allowRichEmbeds,
          previewUrl: activePreviewUrl,
          compact: false,
        });
      }

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
              if (mainEditorApi) mainEditorApi.reset();
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

      // 点赞 / 点踩事件委托
      const reactBtn = e.target.closest('.react-btn');
      if (reactBtn) {
        e.preventDefault();
        if (reactBtn.disabled) return;
        const card = reactBtn.closest('.comment-card');
        if (card && card._reactInFlight) return;
        const cid = reactBtn.getAttribute('data-id');
        const targetVal = parseInt(reactBtn.getAttribute('data-reaction') || '1', 10);
        const wasActive = reactBtn.classList.contains('is-reacted');
        const nextReaction = wasActive ? 0 : targetVal;
        if (card) card._reactInFlight = true;
        const formData = new FormData();
        formData.append('reaction', String(nextReaction));
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
            if (card) card._reactInFlight = false;
            if (ok && data && data.status === 'success' && data.data) {
              const footer = reactBtn.closest('.comment-card-footer');
              if (footer) {
                const likeBtnEl = footer.querySelector('.react-like-btn');
                const dislikeBtnEl = footer.querySelector('.react-dislike-btn');
                const likeSpan = footer.querySelector('.like-num');
                const dislikeSpan = footer.querySelector('.dislike-num');
                if (likeSpan && data.data.like_count !== undefined) likeSpan.textContent = data.data.like_count;
                if (dislikeSpan && data.data.dislike_count !== undefined) dislikeSpan.textContent = data.data.dislike_count;
                if (likeBtnEl) {
                  const activeLike = nextReaction === 1;
                  likeBtnEl.classList.toggle('is-reacted', activeLike);
                  likeBtnEl.setAttribute('aria-pressed', activeLike ? 'true' : 'false');
                }
                if (dislikeBtnEl) {
                  const activeDislike = nextReaction === -1;
                  dislikeBtnEl.classList.toggle('is-reacted', activeDislike);
                  dislikeBtnEl.setAttribute('aria-pressed', activeDislike ? 'true' : 'false');
                }
              }
            } else {
              const errMsg = (data && data.message) || '操作失败，请先登录后重试。';
              showCardFeedback(reactBtn, errMsg, false);
            }
          })
          .catch(() => {
            if (card) card._reactInFlight = false;
            const errMsg = '网络异常，互动操作失败，请稍后重试。';
            showCardFeedback(reactBtn, errMsg, false);
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
        ta.rows = 4;
        ta.required = true;
        ta.maxLength = maxChars;
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

        createCommentEditorUI({
          textarea: ta,
          maxChars,
          allowRichEmbeds,
          previewUrl: activePreviewUrl,
          compact: true,
        });

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
