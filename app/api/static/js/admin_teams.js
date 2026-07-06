/* =============================================================================
   admin_teams.js — прогрессивное обогащение SSR-страницы /admin/teams
   (docs/05 §5, §7; ADR-0016).

   Список команд рендерит СЕРВЕР (единая таблица `admin-teams-table`, <tbody> на
   команду; docs §7). Этот скрипт НЕ фетчит и НЕ рендерит список — он лишь:
     - перехватывает inline-формы мутаций (AJAX без перезагрузки):
         POST   /api/admin/teams              {name}                (создать)
         POST   /api/admin/teams/{id}  + _method=PATCH  {name}      (переименовать)
         POST   /api/admin/teams/{id}  + _method=DELETE            (удалить)
       Без JS формы работают нативно (MethodOverride на сервере, docs §5/§7).
     - открывает JS-диалог назначения лидера:
         GET   /api/admin/users                (кандидаты — участники команды)
         PATCH /api/admin/teams/{id}/leader    {new_leader_user_id}

   После успешной мутации страница перезагружается (SSR — единственный источник
   порядка/состава); флеш переносится через sessionStorage и показывается после
   перезагрузки (как в admin_users.js).

   CSP-безопасно: без inline-скриптов/onclick, события — addEventListener,
   пользовательские данные — textContent. ES2022, без транспиляции.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.SMS) return;
  var SMS = window.SMS;

  var FLASH_KEY = 'sms_admin_flash';

  /* ---- flash через перезагрузку ----------------------------------------- */

  function reloadWithFlash(text, category) {
    try {
      window.sessionStorage.setItem(
        FLASH_KEY,
        JSON.stringify({ text: text, category: category || 'info' })
      );
    } catch (_e) { /* приватный режим — переживём без флеша */ }
    window.location.reload();
  }

  function replayFlash() {
    var raw;
    try {
      raw = window.sessionStorage.getItem(FLASH_KEY);
      if (raw) window.sessionStorage.removeItem(FLASH_KEY);
    } catch (_e) { raw = null; }
    if (!raw) return;
    try {
      var data = JSON.parse(raw);
      if (data && data.text) SMS.flash(data.text, data.category || 'info');
    } catch (_e) { /* игнор */ }
  }

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

  function showError(node, text) {
    if (!node) return;
    node.textContent = text || '';
    node.hidden = !text;
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

  /* ---- создание команды (инлайн-форма в тулбаре) ------------------------- */

  var createForm = document.querySelector('[data-admin-create-team-form]');
  var createError = document.querySelector('[data-admin-create-team-error]');
  var createSubmit = document.querySelector('[data-admin-create-team-submit]');

  if (createForm) {
    createForm.addEventListener('submit', function (event) {
      event.preventDefault();
      showError(createError, '');
      var name = (new FormData(createForm).get('name') || '').toString().trim();
      if (!name) { showError(createError, 'Укажите название команды.'); return; }
      if (createSubmit) createSubmit.disabled = true;
      SMS.csrfFetch('/api/admin/teams', { method: 'POST', body: { name: name } })
        .then(function (resp) {
          if (resp.ok) {
            reloadWithFlash('Команда создана.', 'success');
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) {
            showError(createError, e.message);
            if (createSubmit) createSubmit.disabled = false;
          });
        })
        .catch(function () {
          showError(createError, 'Сетевая ошибка. Попробуйте ещё раз.');
          if (createSubmit) createSubmit.disabled = false;
        });
    });
  }

  /* ---- переименование (инлайн-формы в строках, делегирование) ------------ */

  document.addEventListener('submit', function (event) {
    var form = event.target.closest && event.target.closest('[data-admin-rename-form]');
    if (!form) return;
    event.preventDefault();
    var id = form.getAttribute('data-team-id');
    var input = form.querySelector('input[name="name"]');
    var btn = form.querySelector('button[type="submit"]');
    if (!id || !input) return;
    var name = (input.value || '').toString().trim();
    if (!name) { SMS.flash('Укажите название команды.', 'error'); return; }
    if (btn) btn.disabled = true;
    SMS.csrfFetch('/api/admin/teams/' + encodeURIComponent(id), { method: 'PATCH', body: { name: name } })
      .then(function (resp) {
        if (resp.ok) {
          reloadWithFlash('Команда переименована.', 'success');
          return null;
        }
        return SMS.readJsonError(resp).then(function (e) {
          SMS.flash(e.message, 'error');
          if (btn) btn.disabled = false;
        });
      })
      .catch(function () {
        SMS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
        if (btn) btn.disabled = false;
      });
  });

  /* ---- удаление (инлайн-формы в строках, делегирование) ------------------ */

  document.addEventListener('submit', function (event) {
    var form = event.target.closest && event.target.closest('[data-admin-delete-form]');
    if (!form) return;
    event.preventDefault();
    var id = form.getAttribute('data-team-id');
    var name = form.getAttribute('data-team-name') || '';
    var membersCount = parseInt(form.getAttribute('data-members-count') || '0', 10);
    var btn = form.querySelector('button[type="submit"]');
    if (!id) return;

    var msg = 'Удалить команду «' + name + '»?';
    if (Number.isFinite(membersCount) && membersCount > 0) {
      msg += ' В команде есть участники — сервер отклонит удаление. Сначала переведите/удалите их (раздел «Пользователи»).';
    } else {
      msg += ' Её номера вернутся в нераспределённый пул.';
    }
    if (!window.confirm(msg)) return;

    if (btn) btn.disabled = true;
    SMS.csrfFetch('/api/admin/teams/' + encodeURIComponent(id), { method: 'DELETE' })
      .then(function (resp) {
        if (resp.ok || resp.status === 204) {
          reloadWithFlash('Команда удалена.', 'success');
          return null;
        }
        return SMS.readJsonError(resp).then(function (e) {
          SMS.flash(e.message, 'error');
          if (btn) btn.disabled = false;
        });
      })
      .catch(function () {
        SMS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
        if (btn) btn.disabled = false;
      });
  });

  /* ---- назначение лидера (JS-диалог) ------------------------------------- */

  var leaderDialog = document.querySelector('[data-admin-leader-dialog]');
  var leaderForm = document.querySelector('[data-admin-leader-form]');
  var leaderField = document.querySelector('[data-admin-leader-field]');
  var leaderSelect = document.querySelector('[data-admin-leader-select]');
  var leaderTeamName = document.querySelector('[data-admin-leader-teamname]');
  var leaderError = document.querySelector('[data-admin-leader-error]');
  var leaderEmpty = document.querySelector('[data-admin-leader-empty]');
  var leaderCancel = document.querySelector('[data-admin-leader-cancel]');
  var leaderGo = document.querySelector('[data-admin-leader-go]');

  var pendingLeaderTeamId = 0;

  function showLeaderError(text) { showError(leaderError, text); }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest && event.target.closest('[data-admin-leader-trigger]');
    if (!trigger || !leaderDialog) return;
    var teamId = parseInt(trigger.getAttribute('data-team-id') || '0', 10);
    if (!Number.isFinite(teamId) || teamId < 1) return;
    var teamName = trigger.getAttribute('data-team-name') || '';
    var currentLeaderRaw = trigger.getAttribute('data-leader-id') || '';
    var currentLeaderId = currentLeaderRaw ? parseInt(currentLeaderRaw, 10) : null;

    pendingLeaderTeamId = teamId;
    showLeaderError('');
    if (leaderTeamName) leaderTeamName.textContent = teamName;
    if (leaderSelect) clear(leaderSelect);
    if (leaderField) leaderField.hidden = true;
    if (leaderEmpty) leaderEmpty.hidden = true;
    if (leaderGo) leaderGo.disabled = true;
    openDialog(leaderDialog);

    // Подгрузить ДОМАШНИХ участников этой команды из GET /api/admin/users
    // (users.team_id === teamId — HOME-семантика, docs §5).
    SMS.csrfFetch('/api/admin/users', { method: 'GET' })
      .then(function (resp) {
        if (!resp.ok) return SMS.readJsonError(resp).then(function (e) { throw new Error(e.message); });
        return resp.json();
      })
      .then(function (data) {
        var users = (data && Array.isArray(data.users)) ? data.users : [];
        var members = users.filter(function (u) { return u.team_id === teamId; });
        if (members.length === 0) {
          if (leaderEmpty) leaderEmpty.hidden = false;
          if (leaderField) leaderField.hidden = true;
          if (leaderGo) leaderGo.disabled = true;
          return;
        }
        clear(leaderSelect);
        members.forEach(function (u) {
          var label = u.display_name ? (u.display_name + ' (' + u.username + ')') : u.username;
          var isCurrent = (currentLeaderId != null && u.id === currentLeaderId);
          if (isCurrent) label += ' — текущий лидер';
          var opt = el('option', { text: label, attrs: { value: String(u.id) } });
          if (isCurrent) opt.disabled = true;
          leaderSelect.appendChild(opt);
        });
        // Выбрать первого не-текущего лидера.
        for (var i = 0; i < leaderSelect.options.length; i++) {
          if (!leaderSelect.options[i].disabled) { leaderSelect.selectedIndex = i; break; }
        }
        var hasCandidate = false;
        for (var j = 0; j < leaderSelect.options.length; j++) {
          if (!leaderSelect.options[j].disabled) { hasCandidate = true; break; }
        }
        if (leaderField) leaderField.hidden = false;
        if (leaderEmpty) leaderEmpty.hidden = true;
        if (leaderGo) leaderGo.disabled = !hasCandidate;
      })
      .catch(function (e) {
        showLeaderError(e && e.message ? e.message : 'Не удалось загрузить участников.');
      });
  });

  if (leaderCancel) leaderCancel.addEventListener('click', function () { closeDialog(leaderDialog); });

  if (leaderForm) {
    leaderForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!pendingLeaderTeamId || !leaderSelect) return;
      showLeaderError('');
      var uid = parseInt((leaderSelect.value || '').toString(), 10);
      if (!Number.isFinite(uid) || uid < 1) { showLeaderError('Выберите нового лидера.'); return; }
      if (leaderGo) leaderGo.disabled = true;
      SMS.csrfFetch('/api/admin/teams/' + encodeURIComponent(pendingLeaderTeamId) + '/leader', {
        method: 'PATCH',
        body: { new_leader_user_id: uid }
      })
        .then(function (resp) {
          if (resp.ok) {
            reloadWithFlash('Лидер команды назначен.', 'success');
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) {
            showLeaderError(e.message);
            if (leaderGo) leaderGo.disabled = false;
          });
        })
        .catch(function () {
          showLeaderError('Сетевая ошибка. Попробуйте ещё раз.');
          if (leaderGo) leaderGo.disabled = false;
        });
    });
  }

  /* ---- старт ------------------------------------------------------------- */

  replayFlash();
})();
