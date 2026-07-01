/* =============================================================================
   admin_users.js — страница /admin (docs/05 §4, §7).

   Backend отдаёт SSR-оболочку с пустым контекстом; данные подгружаются здесь:
     - GET  /api/admin/users  -> {users:[{id,username,display_name,role,team_id,
                                  team_name,password_reset_required,
                                  has_telegram_link,created_at,last_login_at}]}
     - GET  /api/admin/teams  -> {teams:[{id,name,...}]}  (для select команд)

   Действия (все через SMS.csrfFetch, double-submit X-CSRF-Token):
     - POST   /api/admin/users            {username,display_name,team_id}
     - POST   /api/admin/users/{id}/reset
     - DELETE /api/admin/users/{id}
     - PATCH  /api/admin/users/{id}       {team_id}

   Состояния корневого контейнера: loading / error(retry) / empty / success.
   CSP-безопасно: DOM через createElement, без innerHTML пользовательских данных,
   события только через addEventListener.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.SMS) return;

  var root = document.querySelector('[data-users-root]');
  if (!root) return;

  var SMS = window.SMS;

  // Кэш команд — используется в select создания/перевода и рендере строк.
  var teamsCache = [];

  /* ---- утилиты DOM ------------------------------------------------------- */

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function el(tag, opts, children) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.className) node.className = opts.className;
    if (opts.text != null) node.textContent = opts.text;
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (k) {
        node.setAttribute(k, opts.attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c) node.appendChild(c);
    });
    return node;
  }

  function roleLabel(role) {
    if (role === 'super_admin') return 'Админ';
    if (role === 'group_leader') return 'Лидер';
    if (role === 'group_member') return 'Участник';
    return role || '';
  }

  function fmtDate(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(0, 16).replace('T', ' ');
    try {
      return d.toLocaleString('ru-RU', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit'
      });
    } catch (_e) {
      return d.toISOString().slice(0, 16).replace('T', ' ');
    }
  }

  /* ---- состояния корня --------------------------------------------------- */

  function renderLoading() {
    clear(root);
    root.appendChild(el('div', { className: 'admin__loading', text: 'Загрузка списка пользователей…', attrs: { role: 'status' } }));
  }

  function renderError(message) {
    clear(root);
    var box = el('div', { className: 'admin__empty', attrs: { role: 'alert' } });
    box.appendChild(el('p', { text: message || 'Не удалось загрузить пользователей.' }));
    var retry = el('button', { className: 'btn btn--secondary', text: 'Повторить', attrs: { type: 'button' } });
    retry.addEventListener('click', loadAll);
    box.appendChild(retry);
    root.appendChild(box);
  }

  function renderEmpty() {
    clear(root);
    var box = el('div', { className: 'admin__empty', attrs: { role: 'status' } });
    box.appendChild(el('p', { text: 'Пользователей пока нет.' }));
    box.appendChild(el('p', { className: 'field__hint', text: 'Нажмите «Создать пользователя», чтобы добавить первого.' }));
    root.appendChild(box);
  }

  /* ---- рендер таблицы ---------------------------------------------------- */

  function teamNameById(id) {
    for (var i = 0; i < teamsCache.length; i++) {
      if (teamsCache[i].id === id) return teamsCache[i].name;
    }
    return null;
  }

  function buildActionsCell(u) {
    var td = el('td', { className: 'admin-users-table__actions' });
    if (u.role === 'super_admin') {
      td.appendChild(el('span', { className: 'admin-users-table__muted', text: 'системный' }));
      return td;
    }

    var resetBtn = el('button', { className: 'btn btn--secondary btn--small', text: 'Сброс', attrs: { type: 'button' } });
    resetBtn.addEventListener('click', function () { onReset(u, resetBtn); });
    td.appendChild(resetBtn);

    var moveBtn = el('button', { className: 'btn btn--secondary btn--small', text: 'Перевести', attrs: { type: 'button' } });
    moveBtn.addEventListener('click', function () { openMoveDialog(u); });
    td.appendChild(moveBtn);

    var delBtn = el('button', { className: 'btn btn--danger btn--small', text: 'Удалить', attrs: { type: 'button' } });
    delBtn.addEventListener('click', function () { openDeleteDialog(u); });
    td.appendChild(delBtn);

    return td;
  }

  function buildRow(u) {
    var isSuper = u.role === 'super_admin';
    var tr = el('tr', { className: 'admin-users-table__row' + (isSuper ? ' admin-users-table__row--system' : '') });

    // Имя
    var nameText = u.display_name ? (u.display_name + ' (' + u.username + ')') : u.username;
    tr.appendChild(el('td', { className: 'admin-users-table__name', text: nameText }));

    // Роль
    var roleTd = el('td', { className: 'admin-users-table__role' });
    roleTd.appendChild(el('span', { className: 'admin-user__badge admin-user__badge--' + (u.role || 'group_member'), text: roleLabel(u.role) }));
    tr.appendChild(roleTd);

    // Команда
    var teamTd = el('td', { className: 'admin-users-table__team' });
    var tname = u.team_name || teamNameById(u.team_id);
    if (tname) {
      teamTd.appendChild(el('span', { className: 'team-chip team-chip--home' }, [el('span', { className: 'team-chip__name', text: tname })]));
    } else {
      teamTd.appendChild(el('span', { className: 'admin-users-table__muted', text: 'без команды' }));
    }
    tr.appendChild(teamTd);

    // Создан
    tr.appendChild(el('td', { className: 'admin-users-table__date', text: fmtDate(u.created_at) || '—' }));

    // Последний вход
    var lastTd = el('td', { className: 'admin-users-table__date' });
    if (u.last_login_at) {
      lastTd.textContent = fmtDate(u.last_login_at);
    } else {
      lastTd.appendChild(el('span', { className: 'admin-users-table__muted', text: 'никогда' }));
    }
    tr.appendChild(lastTd);

    // Telegram
    var tgTd = el('td', { className: 'admin-users-table__tg' });
    if (u.has_telegram_link) {
      tgTd.appendChild(el('span', { className: 'admin-user__badge admin-user__badge--group_leader', text: 'привязан' }));
    } else {
      tgTd.appendChild(el('span', { className: 'admin-users-table__muted', text: '—' }));
    }
    tr.appendChild(tgTd);

    tr.appendChild(buildActionsCell(u));
    return tr;
  }

  function renderList(users) {
    if (!users || users.length === 0) {
      renderEmpty();
      return;
    }
    // Сортировка: сначала по команде (null в конец), затем по логину.
    var sorted = users.slice().sort(function (a, b) {
      var at = a.team_name || teamNameById(a.team_id) || '￿';
      var bt = b.team_name || teamNameById(b.team_id) || '￿';
      if (at !== bt) return at < bt ? -1 : 1;
      return (a.username || '') < (b.username || '') ? -1 : 1;
    });

    clear(root);
    var wrapper = el('div', { className: 'table-wrapper' });
    var table = el('table', { className: 'admin-users-table' });

    var thead = el('thead');
    var htr = el('tr');
    ['Имя', 'Роль', 'Команда', 'Создан', 'Последний вход', 'Telegram', 'Действия'].forEach(function (h) {
      htr.appendChild(el('th', { text: h, attrs: { scope: 'col' } }));
    });
    thead.appendChild(htr);
    table.appendChild(thead);

    var tbody = el('tbody');
    sorted.forEach(function (u) { tbody.appendChild(buildRow(u)); });
    table.appendChild(tbody);

    wrapper.appendChild(table);
    root.appendChild(wrapper);
  }

  /* ---- загрузка данных --------------------------------------------------- */

  function loadTeams() {
    return SMS.csrfFetch('/api/admin/teams', { method: 'GET' }).then(function (resp) {
      if (!resp.ok) return SMS.readJsonError(resp).then(function (e) { throw new Error(e.message); });
      return resp.json();
    }).then(function (data) {
      teamsCache = (data && Array.isArray(data.teams)) ? data.teams : [];
      return teamsCache;
    });
  }

  function loadUsers() {
    return SMS.csrfFetch('/api/admin/users', { method: 'GET' }).then(function (resp) {
      if (!resp.ok) return SMS.readJsonError(resp).then(function (e) { throw new Error(e.message); });
      return resp.json();
    }).then(function (data) {
      return (data && Array.isArray(data.users)) ? data.users : [];
    });
  }

  function loadAll() {
    renderLoading();
    Promise.all([loadTeams(), loadUsers()])
      .then(function (res) {
        populateCreateTeamSelect();
        renderList(res[1]);
      })
      .catch(function (e) {
        renderError(e && e.message ? e.message : 'Сетевая ошибка.');
      });
  }

  /* ---- создание пользователя -------------------------------------------- */

  var createBtn = document.querySelector('[data-admin-create-user]');
  var createDialog = document.querySelector('[data-admin-create-dialog]');
  var createForm = document.querySelector('[data-admin-create-user-form]');
  var createError = document.querySelector('[data-admin-create-error]');
  var createSubmit = document.querySelector('[data-admin-create-submit]');
  var teamSelect = document.querySelector('[data-admin-team-select]');
  var teamHint = document.querySelector('[data-admin-team-hint]');
  var teamEmptyHint = document.querySelector('[data-admin-team-empty]');

  function fillTeamOptions(select, opts) {
    opts = opts || {};
    clear(select);
    if (opts.placeholder) {
      var ph = el('option', { text: opts.placeholder, attrs: { value: '' } });
      select.appendChild(ph);
    }
    teamsCache.forEach(function (t) {
      select.appendChild(el('option', { text: t.name, attrs: { value: String(t.id) } }));
    });
  }

  function populateCreateTeamSelect() {
    if (!teamSelect) return;
    fillTeamOptions(teamSelect, { placeholder: '— выберите команду —' });
    var hasTeams = teamsCache.length > 0;
    if (teamHint) teamHint.hidden = !hasTeams;
    if (teamEmptyHint) teamEmptyHint.hidden = hasTeams;
    if (createSubmit) createSubmit.disabled = !hasTeams;
  }

  function showCreateError(text) {
    if (!createError) return;
    createError.textContent = text || '';
    createError.hidden = !text;
  }

  function openDialog(dlg) {
    if (!dlg) return;
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', 'open');
  }
  function closeDialog(dlg) {
    if (!dlg) return;
    if (typeof dlg.close === 'function') dlg.close();
    else dlg.removeAttribute('open');
  }

  if (createBtn && createDialog) {
    createBtn.addEventListener('click', function () {
      showCreateError('');
      if (createForm) createForm.reset();
      populateCreateTeamSelect();
      openDialog(createDialog);
    });
  }

  if (createForm) {
    createForm.addEventListener('submit', function (event) {
      event.preventDefault();
      showCreateError('');
      var fd = new FormData(createForm);
      var username = (fd.get('username') || '').toString().trim();
      var displayName = (fd.get('display_name') || '').toString().trim();
      var teamRaw = (fd.get('team_id') || '').toString().trim();

      if (!username) { showCreateError('Укажите логин.'); return; }
      if (!teamRaw) { showCreateError('Выберите команду.'); return; }
      var teamId = parseInt(teamRaw, 10);
      if (!Number.isFinite(teamId) || teamId < 1) { showCreateError('Выберите команду.'); return; }

      var payload = { username: username, team_id: teamId };
      payload.display_name = displayName ? displayName : null;

      if (createSubmit) createSubmit.disabled = true;
      SMS.csrfFetch('/api/admin/users', { method: 'POST', body: payload })
        .then(function (resp) {
          if (resp.ok) {
            SMS.flash('Пользователь создан. Сообщите логин — при первом входе он задаст пароль.', 'success');
            closeDialog(createDialog);
            loadAll();
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) { showCreateError(e.message); });
        })
        .catch(function () { showCreateError('Сетевая ошибка. Попробуйте ещё раз.'); })
        .then(function () { if (createSubmit) createSubmit.disabled = false; });
    });
  }

  /* ---- сброс пароля ------------------------------------------------------ */

  function onReset(u, btn) {
    if (!window.confirm('Сбросить пароль пользователю ' + u.username + '? При следующем входе он задаст новый пароль, все его сессии завершатся.')) return;
    btn.disabled = true;
    SMS.csrfFetch('/api/admin/users/' + u.id + '/reset', { method: 'POST' })
      .then(function (resp) {
        if (resp.ok) {
          SMS.flash('Пароль сброшен. Сессии и Telegram-привязки пользователя завершены.', 'success');
          loadAll();
          return null;
        }
        return SMS.readJsonError(resp).then(function (e) { SMS.flash(e.message, 'error'); });
      })
      .catch(function () { SMS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error'); })
      .then(function () { btn.disabled = false; });
  }

  /* ---- перевод в другую команду ----------------------------------------- */

  var moveDialog = document.querySelector('[data-admin-move-dialog]');
  var moveForm = document.querySelector('[data-admin-move-form]');
  var moveSelect = document.querySelector('[data-admin-move-select]');
  var moveUsername = document.querySelector('[data-admin-move-username]');
  var moveError = document.querySelector('[data-admin-move-error]');
  var moveCancel = document.querySelector('[data-admin-move-cancel]');
  var moveGo = document.querySelector('[data-admin-move-go]');
  var pendingMoveUser = null;

  function showMoveError(text) {
    if (!moveError) return;
    moveError.textContent = text || '';
    moveError.hidden = !text;
  }

  function openMoveDialog(u) {
    pendingMoveUser = u;
    if (!moveSelect) return;
    showMoveError('');
    fillTeamOptions(moveSelect, { placeholder: '— выберите команду —' });
    // Предвыбрать текущую команду.
    if (u.team_id) moveSelect.value = String(u.team_id);
    if (moveUsername) moveUsername.textContent = u.username;
    openDialog(moveDialog);
  }

  if (moveCancel) moveCancel.addEventListener('click', function () { closeDialog(moveDialog); });

  if (moveForm) {
    moveForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!pendingMoveUser || !moveSelect) return;
      showMoveError('');
      var teamId = parseInt((moveSelect.value || '').toString(), 10);
      if (!Number.isFinite(teamId) || teamId < 1) { showMoveError('Выберите команду.'); return; }
      if (teamId === pendingMoveUser.team_id) { showMoveError('Пользователь уже в этой команде.'); return; }
      if (moveGo) moveGo.disabled = true;
      SMS.csrfFetch('/api/admin/users/' + pendingMoveUser.id, { method: 'PATCH', body: { team_id: teamId } })
        .then(function (resp) {
          if (resp.ok) {
            SMS.flash('Пользователь переведён в другую команду.', 'success');
            closeDialog(moveDialog);
            loadAll();
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) { showMoveError(e.message); });
        })
        .catch(function () { showMoveError('Сетевая ошибка. Попробуйте ещё раз.'); })
        .then(function () { if (moveGo) moveGo.disabled = false; });
    });
  }

  /* ---- удаление (подтверждение логином) --------------------------------- */

  var deleteDialog = document.querySelector('[data-admin-delete-dialog]');
  var deleteUsernameLabel = document.querySelector('[data-admin-delete-username]');
  var deleteConfirmForm = document.querySelector('[data-admin-delete-confirm-form]');
  var deleteConfirmInput = document.getElementById('delete-confirm-input');
  var deleteGo = document.querySelector('[data-admin-delete-go]');
  var deleteCancel = document.querySelector('[data-admin-delete-cancel]');
  var deleteError = document.querySelector('[data-admin-delete-error]');
  var pendingDeleteUser = null;

  function showDeleteError(text) {
    if (!deleteError) return;
    deleteError.textContent = text || '';
    deleteError.hidden = !text;
  }

  function openDeleteDialog(u) {
    pendingDeleteUser = u;
    showDeleteError('');
    if (deleteUsernameLabel) deleteUsernameLabel.textContent = u.username;
    if (deleteConfirmInput) { deleteConfirmInput.value = ''; }
    if (deleteGo) deleteGo.disabled = true;
    openDialog(deleteDialog);
    if (deleteConfirmInput) deleteConfirmInput.focus();
  }

  if (deleteConfirmInput && deleteGo) {
    deleteConfirmInput.addEventListener('input', function () {
      deleteGo.disabled = !pendingDeleteUser || deleteConfirmInput.value !== pendingDeleteUser.username;
    });
  }

  if (deleteCancel) deleteCancel.addEventListener('click', function () { closeDialog(deleteDialog); });

  if (deleteConfirmForm) {
    deleteConfirmForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!pendingDeleteUser) return;
      if (!deleteConfirmInput || deleteConfirmInput.value !== pendingDeleteUser.username) return;
      showDeleteError('');
      if (deleteGo) deleteGo.disabled = true;
      SMS.csrfFetch('/api/admin/users/' + pendingDeleteUser.id, { method: 'DELETE' })
        .then(function (resp) {
          if (resp.ok) {
            SMS.flash('Пользователь удалён.', 'success');
            closeDialog(deleteDialog);
            loadAll();
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) { showDeleteError(e.message); });
        })
        .catch(function () { showDeleteError('Сетевая ошибка. Попробуйте ещё раз.'); })
        .then(function () { if (deleteGo) deleteGo.disabled = false; });
    });
  }

  /* ---- старт ------------------------------------------------------------- */

  loadAll();
})();
