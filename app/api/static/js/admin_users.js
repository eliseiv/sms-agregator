/* =============================================================================
   admin_users.js — страница /admin (docs/05 §4, §5, §7; ADR-0015).

   Данные пользователей рендерит СЕРВЕР (единая таблица `admin-users-table` с
   `<tbody>`-бандингом по командам; один пользователь = одна строка). Этот
   скрипт — прогрессивное улучшение: навешивает обработчики на SSR-элементы и
   выполняет изменяющие запросы через SMS.csrfFetch (double-submit X-CSRF-Token):

     - POST   /api/admin/users            {username, display_name, team_id}
     - POST   /api/admin/teams            {name}
     - POST   /api/admin/users/{id}/reset
     - DELETE /api/admin/users/{id}

   Управление командами пользователя («+»-меню: перевод / доп. членство /
   удаление членства) — отдельный файл admin_memberships.js.

   После успешной мутации страница перезагружается (location.reload) — серверная
   группировка (docs §7) остаётся единственным источником порядка. Сообщение об
   успехе переносится через sessionStorage и показывается после перезагрузки.

   CSP-безопасно: без inline-скриптов/onclick, DOM только через API,
   события только через addEventListener, пользовательские данные — textContent.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.SMS) return;
  var SMS = window.SMS;

  var FLASH_KEY = 'sms_admin_flash';

  /* ---- flash через перезагрузку ----------------------------------------- */

  function flashAfterReload(text, category) {
    try {
      window.sessionStorage.setItem(FLASH_KEY, JSON.stringify({ text: text, category: category || 'info' }));
    } catch (_e) { /* приватный режим — переживём без флеша */ }
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

  function reloadWithFlash(text, category) {
    flashAfterReload(text, category);
    window.location.reload();
  }

  /* ---- диалоги ----------------------------------------------------------- */

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

  function showError(node, text) {
    if (!node) return;
    node.textContent = text || '';
    node.hidden = !text;
  }

  /* ---- создание пользователя -------------------------------------------- */

  var createBtn = document.querySelector('[data-admin-create-user]');
  var createDialog = document.querySelector('[data-admin-create-dialog]');
  var createForm = document.querySelector('[data-admin-create-user-form]');
  var createError = document.querySelector('[data-admin-create-error]');
  var createSubmit = document.querySelector('[data-admin-create-submit]');

  if (createBtn && createDialog) {
    createBtn.addEventListener('click', function () {
      showError(createError, '');
      if (createForm) createForm.reset();
      openDialog(createDialog);
    });
  }

  if (createForm) {
    createForm.addEventListener('submit', function (event) {
      event.preventDefault();
      showError(createError, '');
      var fd = new FormData(createForm);
      var username = (fd.get('username') || '').toString().trim();
      var displayName = (fd.get('display_name') || '').toString().trim();
      var teamRaw = (fd.get('team_id') || '').toString().trim();

      if (!username) { showError(createError, 'Укажите логин.'); return; }
      if (!teamRaw) { showError(createError, 'Выберите команду.'); return; }
      var teamId = parseInt(teamRaw, 10);
      if (!Number.isFinite(teamId) || teamId < 1) { showError(createError, 'Выберите команду.'); return; }

      var payload = { username: username, team_id: teamId, display_name: displayName ? displayName : null };

      // Доп. команды (multi-team, ADR-0012, docs §4): выбранные опции мультиселекта
      // отправляем массивом extra_team_ids. Домашняя команда исключается (backend
      // тоже дедуплит/исключает её молча); дубли убираются.
      var extraSelect = createForm.querySelector('[data-admin-create-extra-teams]');
      if (extraSelect) {
        var extraIds = [];
        for (var i = 0; i < extraSelect.options.length; i++) {
          var opt = extraSelect.options[i];
          if (!opt.selected) continue;
          var v = parseInt(opt.value, 10);
          if (Number.isFinite(v) && v > 0 && v !== teamId && extraIds.indexOf(v) === -1) {
            extraIds.push(v);
          }
        }
        if (extraIds.length) payload.extra_team_ids = extraIds;
      }

      if (createSubmit) createSubmit.disabled = true;
      SMS.csrfFetch('/api/admin/users', { method: 'POST', body: payload })
        .then(function (resp) {
          if (resp.ok) {
            reloadWithFlash('Пользователь создан. Сообщите логин — при первом входе он задаст пароль.', 'success');
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

  /* ---- создание команды (упрощённый диалог, docs §5/§7) ----------------- */

  var createTeamBtn = document.querySelector('[data-admin-create-team]');
  var createTeamDialog = document.querySelector('[data-admin-create-team-dialog]');
  var createTeamForm = document.querySelector('[data-admin-create-team-form]');
  var createTeamError = document.querySelector('[data-admin-create-team-error]');
  var createTeamSubmit = document.querySelector('[data-admin-create-team-submit]');

  if (createTeamBtn && createTeamDialog) {
    createTeamBtn.addEventListener('click', function () {
      showError(createTeamError, '');
      if (createTeamForm) createTeamForm.reset();
      openDialog(createTeamDialog);
    });
  }

  if (createTeamForm) {
    createTeamForm.addEventListener('submit', function (event) {
      event.preventDefault();
      showError(createTeamError, '');
      var fd = new FormData(createTeamForm);
      var name = (fd.get('name') || '').toString().trim();
      if (!name) { showError(createTeamError, 'Укажите название команды.'); return; }

      if (createTeamSubmit) createTeamSubmit.disabled = true;
      SMS.csrfFetch('/api/admin/teams', { method: 'POST', body: { name: name } })
        .then(function (resp) {
          if (resp.ok) {
            reloadWithFlash('Команда создана.', 'success');
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) {
            showError(createTeamError, e.message);
            if (createTeamSubmit) createTeamSubmit.disabled = false;
          });
        })
        .catch(function () {
          showError(createTeamError, 'Сетевая ошибка. Попробуйте ещё раз.');
          if (createTeamSubmit) createTeamSubmit.disabled = false;
        });
    });
  }

  /* ---- сброс пароля (делегирование) ------------------------------------- */

  document.addEventListener('click', function (event) {
    var btn = event.target.closest && event.target.closest('[data-admin-reset]');
    if (!btn) return;
    var id = btn.getAttribute('data-user-id');
    var username = btn.getAttribute('data-username') || '';
    if (!id) return;
    if (!window.confirm('Сбросить пароль пользователю ' + username + '? При следующем входе он задаст новый пароль, все его сессии и Telegram-привязки завершатся.')) return;
    btn.disabled = true;
    SMS.csrfFetch('/api/admin/users/' + encodeURIComponent(id) + '/reset', { method: 'POST' })
      .then(function (resp) {
        if (resp.ok) {
          reloadWithFlash('Пароль сброшен. Сессии и Telegram-привязки пользователя завершены.', 'success');
          return null;
        }
        return SMS.readJsonError(resp).then(function (e) {
          SMS.flash(e.message, 'error');
          btn.disabled = false;
        });
      })
      .catch(function () {
        SMS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
        btn.disabled = false;
      });
  });

  /* ---- удаление (подтверждение логином) --------------------------------- */

  var deleteDialog = document.querySelector('[data-admin-delete-dialog]');
  var deleteUsernameLabel = document.querySelector('[data-admin-delete-username]');
  var deleteConfirmForm = document.querySelector('[data-admin-delete-confirm-form]');
  var deleteConfirmInput = document.getElementById('delete-confirm-input');
  var deleteGo = document.querySelector('[data-admin-delete-go]');
  var deleteCancel = document.querySelector('[data-admin-delete-cancel]');
  var deleteError = document.querySelector('[data-admin-delete-error]');
  var pendingDelete = null;

  document.addEventListener('click', function (event) {
    var btn = event.target.closest && event.target.closest('[data-admin-delete]');
    if (!btn || !deleteDialog) return;
    pendingDelete = {
      id: btn.getAttribute('data-user-id'),
      username: btn.getAttribute('data-username') || ''
    };
    showError(deleteError, '');
    if (deleteUsernameLabel) deleteUsernameLabel.textContent = pendingDelete.username;
    if (deleteConfirmInput) deleteConfirmInput.value = '';
    if (deleteGo) deleteGo.disabled = true;
    openDialog(deleteDialog);
    if (deleteConfirmInput) deleteConfirmInput.focus();
  });

  if (deleteConfirmInput && deleteGo) {
    deleteConfirmInput.addEventListener('input', function () {
      deleteGo.disabled = !pendingDelete || deleteConfirmInput.value !== pendingDelete.username;
    });
  }

  if (deleteCancel) deleteCancel.addEventListener('click', function () { closeDialog(deleteDialog); });

  if (deleteConfirmForm) {
    deleteConfirmForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!pendingDelete) return;
      if (!deleteConfirmInput || deleteConfirmInput.value !== pendingDelete.username) return;
      showError(deleteError, '');
      if (deleteGo) deleteGo.disabled = true;
      SMS.csrfFetch('/api/admin/users/' + encodeURIComponent(pendingDelete.id), { method: 'DELETE' })
        .then(function (resp) {
          if (resp.ok || resp.status === 204) {
            reloadWithFlash('Пользователь удалён.', 'success');
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) {
            showError(deleteError, e.message);
            if (deleteGo) deleteGo.disabled = false;
          });
        })
        .catch(function () {
          showError(deleteError, 'Сетевая ошибка. Попробуйте ещё раз.');
          if (deleteGo) deleteGo.disabled = false;
        });
    });
  }

  /* ---- старт ------------------------------------------------------------- */

  replayFlash();
})();
