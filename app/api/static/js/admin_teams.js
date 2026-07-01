/* =============================================================================
   admin_teams.js — страница /admin/teams (docs/05 §5, §7).

   Backend отдаёт SSR-оболочку с пустым контекстом; данные подгружаются здесь:
     - GET  /api/admin/teams  -> {teams:[{id,name,leader_user_id,leader_username,
                                   members_count,numbers_count,is_active,created_at}]}
     - GET  /api/admin/users  -> {users:[...]}  (для выбора нового лидера)

   Действия (через SMS.csrfFetch, double-submit X-CSRF-Token):
     - POST   /api/admin/teams              {name}
     - PATCH  /api/admin/teams/{id}         {name}
     - PATCH  /api/admin/teams/{id}/leader  {new_leader_user_id}
     - DELETE /api/admin/teams/{id}         (только пустую → иначе team_has_members)

   Состояния корня: loading / error(retry) / empty / success. CSP-безопасно.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.SMS) return;

  var root = document.querySelector('[data-teams-root]');
  if (!root) return;

  var SMS = window.SMS;

  /* ---- утилиты ----------------------------------------------------------- */

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function el(tag, opts, children) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.className) node.className = opts.className;
    if (opts.text != null) node.textContent = opts.text;
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (k) { node.setAttribute(k, opts.attrs[k]); });
    }
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
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

  /* ---- состояния корня --------------------------------------------------- */

  function renderLoading() {
    clear(root);
    root.appendChild(el('div', { className: 'admin__loading', text: 'Загрузка списка команд…', attrs: { role: 'status' } }));
  }

  function renderError(message) {
    clear(root);
    var box = el('div', { className: 'admin__empty', attrs: { role: 'alert' } });
    box.appendChild(el('p', { text: message || 'Не удалось загрузить команды.' }));
    var retry = el('button', { className: 'btn btn--secondary', text: 'Повторить', attrs: { type: 'button' } });
    retry.addEventListener('click', loadTeams);
    box.appendChild(retry);
    root.appendChild(box);
  }

  function renderEmpty() {
    clear(root);
    var box = el('div', { className: 'admin__empty', attrs: { role: 'status' } });
    box.appendChild(el('p', { text: 'Команд пока нет.' }));
    box.appendChild(el('p', { className: 'field__hint', text: 'Нажмите «Создать команду», чтобы добавить первую.' }));
    root.appendChild(box);
  }

  /* ---- рендер таблицы ---------------------------------------------------- */

  function buildActionsCell(t) {
    var td = el('td', { className: 'admin-groups-table__actions' });

    var renameBtn = el('button', { className: 'btn btn--secondary btn--small', text: 'Переименовать', attrs: { type: 'button' } });
    renameBtn.addEventListener('click', function () { openRenameDialog(t); });
    td.appendChild(renameBtn);

    var leaderBtn = el('button', { className: 'btn btn--secondary btn--small', text: 'Лидер', attrs: { type: 'button' } });
    leaderBtn.addEventListener('click', function () { openLeaderDialog(t); });
    td.appendChild(leaderBtn);

    var delBtn = el('button', { className: 'btn btn--danger btn--small', text: 'Удалить', attrs: { type: 'button' } });
    delBtn.addEventListener('click', function () { onDelete(t, delBtn); });
    td.appendChild(delBtn);

    return td;
  }

  function buildRow(t) {
    var tr = el('tr', { className: 'admin-groups-table__row' });

    tr.appendChild(el('td', { className: 'admin-groups-table__name' }, [el('strong', { text: t.name })]));

    var leaderTd = el('td', { className: 'admin-groups-table__leader' });
    if (t.leader_username) {
      leaderTd.textContent = t.leader_username;
    } else {
      leaderTd.appendChild(el('span', { className: 'admin-users-table__muted', text: '—' }));
    }
    tr.appendChild(leaderTd);

    tr.appendChild(el('td', { className: 'admin-groups-table__members', text: String(t.members_count != null ? t.members_count : 0) }));
    tr.appendChild(el('td', { className: 'admin-groups-table__numbers', text: String(t.numbers_count != null ? t.numbers_count : 0) }));
    tr.appendChild(el('td', { className: 'admin-groups-table__date', text: fmtDate(t.created_at) || '—' }));
    tr.appendChild(buildActionsCell(t));

    return tr;
  }

  function renderList(teams) {
    if (!teams || teams.length === 0) { renderEmpty(); return; }
    var sorted = teams.slice().sort(function (a, b) {
      return (a.name || '') < (b.name || '') ? -1 : 1;
    });

    clear(root);
    var wrapper = el('div', { className: 'table-wrapper' });
    var table = el('table', { className: 'admin-groups-table' });

    var thead = el('thead');
    var htr = el('tr');
    ['Название', 'Лидер', 'Участников', 'Номеров', 'Создана', 'Действия'].forEach(function (h) {
      htr.appendChild(el('th', { text: h, attrs: { scope: 'col' } }));
    });
    thead.appendChild(htr);
    table.appendChild(thead);

    var tbody = el('tbody');
    sorted.forEach(function (t) { tbody.appendChild(buildRow(t)); });
    table.appendChild(tbody);

    wrapper.appendChild(table);
    root.appendChild(wrapper);
  }

  /* ---- загрузка ---------------------------------------------------------- */

  function loadTeams() {
    renderLoading();
    SMS.csrfFetch('/api/admin/teams', { method: 'GET' })
      .then(function (resp) {
        if (!resp.ok) return SMS.readJsonError(resp).then(function (e) { throw new Error(e.message); });
        return resp.json();
      })
      .then(function (data) {
        renderList((data && Array.isArray(data.teams)) ? data.teams : []);
      })
      .catch(function (e) {
        renderError(e && e.message ? e.message : 'Сетевая ошибка.');
      });
  }

  /* ---- создание команды -------------------------------------------------- */

  var createBtn = document.querySelector('[data-admin-create-team]');
  var createDialog = document.querySelector('[data-admin-create-team-dialog]');
  var createForm = document.querySelector('[data-admin-create-team-form]');
  var createError = document.querySelector('[data-admin-create-team-error]');
  var createSubmit = document.querySelector('[data-admin-create-team-submit]');

  function showCreateError(text) {
    if (!createError) return;
    createError.textContent = text || '';
    createError.hidden = !text;
  }

  if (createBtn && createDialog) {
    createBtn.addEventListener('click', function () {
      showCreateError('');
      if (createForm) createForm.reset();
      openDialog(createDialog);
    });
  }

  if (createForm) {
    createForm.addEventListener('submit', function (event) {
      event.preventDefault();
      showCreateError('');
      var name = (new FormData(createForm).get('name') || '').toString().trim();
      if (!name) { showCreateError('Укажите название команды.'); return; }
      if (createSubmit) createSubmit.disabled = true;
      SMS.csrfFetch('/api/admin/teams', { method: 'POST', body: { name: name } })
        .then(function (resp) {
          if (resp.ok) {
            SMS.flash('Команда создана.', 'success');
            closeDialog(createDialog);
            loadTeams();
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) { showCreateError(e.message); });
        })
        .catch(function () { showCreateError('Сетевая ошибка. Попробуйте ещё раз.'); })
        .then(function () { if (createSubmit) createSubmit.disabled = false; });
    });
  }

  /* ---- переименование ---------------------------------------------------- */

  var renameDialog = document.querySelector('[data-admin-rename-dialog]');
  var renameForm = document.querySelector('[data-admin-rename-form]');
  var renameError = document.querySelector('[data-admin-rename-error]');
  var renameCancel = document.querySelector('[data-admin-rename-cancel]');
  var renameGo = document.querySelector('[data-admin-rename-go]');
  var renameInput = document.getElementById('rename-team-name');
  var pendingRenameTeam = null;

  function showRenameError(text) {
    if (!renameError) return;
    renameError.textContent = text || '';
    renameError.hidden = !text;
  }

  function openRenameDialog(t) {
    pendingRenameTeam = t;
    showRenameError('');
    if (renameInput) renameInput.value = t.name || '';
    openDialog(renameDialog);
    if (renameInput) renameInput.focus();
  }

  if (renameCancel) renameCancel.addEventListener('click', function () { closeDialog(renameDialog); });

  if (renameForm) {
    renameForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!pendingRenameTeam) return;
      showRenameError('');
      var name = (new FormData(renameForm).get('name') || '').toString().trim();
      if (!name) { showRenameError('Укажите название команды.'); return; }
      if (renameGo) renameGo.disabled = true;
      SMS.csrfFetch('/api/admin/teams/' + pendingRenameTeam.id, { method: 'PATCH', body: { name: name } })
        .then(function (resp) {
          if (resp.ok) {
            SMS.flash('Команда переименована.', 'success');
            closeDialog(renameDialog);
            loadTeams();
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) { showRenameError(e.message); });
        })
        .catch(function () { showRenameError('Сетевая ошибка. Попробуйте ещё раз.'); })
        .then(function () { if (renameGo) renameGo.disabled = false; });
    });
  }

  /* ---- переназначение лидера --------------------------------------------- */

  var leaderDialog = document.querySelector('[data-admin-leader-dialog]');
  var leaderForm = document.querySelector('[data-admin-leader-form]');
  var leaderField = document.querySelector('[data-admin-leader-field]');
  var leaderSelect = document.querySelector('[data-admin-leader-select]');
  var leaderTeamName = document.querySelector('[data-admin-leader-teamname]');
  var leaderError = document.querySelector('[data-admin-leader-error]');
  var leaderEmpty = document.querySelector('[data-admin-leader-empty]');
  var leaderCancel = document.querySelector('[data-admin-leader-cancel]');
  var leaderGo = document.querySelector('[data-admin-leader-go]');
  var pendingLeaderTeam = null;

  function showLeaderError(text) {
    if (!leaderError) return;
    leaderError.textContent = text || '';
    leaderError.hidden = !text;
  }

  function openLeaderDialog(t) {
    pendingLeaderTeam = t;
    showLeaderError('');
    if (leaderTeamName) leaderTeamName.textContent = t.name || '';
    if (leaderSelect) clear(leaderSelect);
    if (leaderField) leaderField.hidden = true;
    if (leaderEmpty) leaderEmpty.hidden = true;
    if (leaderGo) leaderGo.disabled = true;
    openDialog(leaderDialog);

    // Подгрузить участников этой команды из GET /api/admin/users.
    SMS.csrfFetch('/api/admin/users', { method: 'GET' })
      .then(function (resp) {
        if (!resp.ok) return SMS.readJsonError(resp).then(function (e) { throw new Error(e.message); });
        return resp.json();
      })
      .then(function (data) {
        var users = (data && Array.isArray(data.users)) ? data.users : [];
        var members = users.filter(function (u) { return u.team_id === t.id; });
        if (members.length === 0) {
          if (leaderEmpty) leaderEmpty.hidden = false;
          if (leaderField) leaderField.hidden = true;
          if (leaderGo) leaderGo.disabled = true;
          return;
        }
        clear(leaderSelect);
        members.forEach(function (u) {
          var label = u.display_name ? (u.display_name + ' (' + u.username + ')') : u.username;
          if (u.id === t.leader_user_id) label += ' — текущий лидер';
          var opt = el('option', { text: label, attrs: { value: String(u.id) } });
          if (u.id === t.leader_user_id) opt.disabled = true;
          leaderSelect.appendChild(opt);
        });
        // Выбрать первого не-текущего лидера.
        for (var i = 0; i < leaderSelect.options.length; i++) {
          if (!leaderSelect.options[i].disabled) { leaderSelect.selectedIndex = i; break; }
        }
        if (leaderField) leaderField.hidden = false;
        if (leaderEmpty) leaderEmpty.hidden = true;
        if (leaderGo) leaderGo.disabled = false;
      })
      .catch(function (e) {
        showLeaderError(e && e.message ? e.message : 'Не удалось загрузить участников.');
      });
  }

  if (leaderCancel) leaderCancel.addEventListener('click', function () { closeDialog(leaderDialog); });

  if (leaderForm) {
    leaderForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!pendingLeaderTeam || !leaderSelect) return;
      showLeaderError('');
      var uid = parseInt((leaderSelect.value || '').toString(), 10);
      if (!Number.isFinite(uid) || uid < 1) { showLeaderError('Выберите нового лидера.'); return; }
      if (leaderGo) leaderGo.disabled = true;
      SMS.csrfFetch('/api/admin/teams/' + pendingLeaderTeam.id + '/leader', { method: 'PATCH', body: { new_leader_user_id: uid } })
        .then(function (resp) {
          if (resp.ok) {
            SMS.flash('Лидер команды назначен.', 'success');
            closeDialog(leaderDialog);
            loadTeams();
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) { showLeaderError(e.message); });
        })
        .catch(function () { showLeaderError('Сетевая ошибка. Попробуйте ещё раз.'); })
        .then(function () { if (leaderGo) leaderGo.disabled = false; });
    });
  }

  /* ---- удаление ---------------------------------------------------------- */

  function onDelete(t, btn) {
    var msg = 'Удалить команду «' + t.name + '»?';
    if (t.members_count && t.members_count > 0) {
      msg += ' В команде есть участники — сервер отклонит удаление. Сначала переведите/удалите их.';
    }
    if (!window.confirm(msg)) return;
    btn.disabled = true;
    SMS.csrfFetch('/api/admin/teams/' + t.id, { method: 'DELETE' })
      .then(function (resp) {
        if (resp.ok) {
          SMS.flash('Команда удалена.', 'success');
          loadTeams();
          return null;
        }
        return SMS.readJsonError(resp).then(function (e) { SMS.flash(e.message, 'error'); });
      })
      .catch(function () { SMS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error'); })
      .then(function () { btn.disabled = false; });
  }

  /* ---- старт ------------------------------------------------------------- */

  loadTeams();
})();
