/* =============================================================================
   admin_memberships.js — управление членством пользователя в командах на /admin
   (multi-team, ADR-0012, docs/05 §4).

   Прогрессивное улучшение SSR-разметки users.html. Endpoints:
     - POST   /api/admin/users/{id}/teams          {team_id}  — добавить доп. членство
     - DELETE /api/admin/users/{id}/teams/{team_id}           — убрать доп. членство

   Add: кнопка [data-admin-add-team] открывает диалог [data-admin-add-dialog];
   список команд исключает те, где пользователь уже состоит (data-member-team-ids).
   Remove: форма [data-admin-remove-membership] (no-JS fallback — POST на ТОТ ЖЕ
   путь ресурса /api/admin/users/{id}/teams/{team_id} + _method=DELETE, без
   /delete-суффикса) перехватывается — confirm + DELETE через SMS.csrfFetch.

   Коды ошибок (cannot_add_super_admin_to_team, membership_already_exists,
   cannot_remove_home_membership, membership_not_found, team_not_found) —
   человекочитаемо через SMS.readJsonError/ERROR_MAP (csrf.js). После успеха —
   reload (SSR-группировка остаётся единственным источником порядка); флеш
   переносится через sessionStorage (совместимо с admin_users.js replayFlash).

   CSP-безопасно: без inline-скриптов/onclick, события — addEventListener,
   пользовательские данные — textContent.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.SMS) return;
  var SMS = window.SMS;

  // Тот же ключ, что читает admin_users.js::replayFlash после перезагрузки.
  var FLASH_KEY = 'sms_admin_flash';

  function reloadWithFlash(text, category) {
    try {
      window.sessionStorage.setItem(
        FLASH_KEY,
        JSON.stringify({ text: text, category: category || 'info' })
      );
    } catch (_e) { /* приватный режим — переживём без флеша */ }
    window.location.reload();
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
  function showError(node, text) {
    if (!node) return;
    node.textContent = text || '';
    node.hidden = !text;
  }

  /* ---- добавление доп. членства ----------------------------------------- */

  var addDialog = document.querySelector('[data-admin-add-dialog]');
  var addForm = document.querySelector('[data-admin-add-form]');
  var addSelect = document.querySelector('[data-admin-add-select]');
  var addField = document.querySelector('[data-admin-add-field]');
  var addUsername = document.querySelector('[data-admin-add-username]');
  var addEmpty = document.querySelector('[data-admin-add-empty]');
  var addCancel = document.querySelector('[data-admin-add-cancel]');
  var addGo = document.querySelector('[data-admin-add-go]');
  var addError = document.querySelector('[data-admin-add-error]');

  var pendingAddUserId = null;

  // Полный набор опций команд захватываем один раз — на каждое открытие
  // пересобираем select, исключая команды пользователя.
  var allTeamOptions = [];
  if (addSelect) {
    allTeamOptions = Array.prototype.slice.call(addSelect.options).map(function (opt) {
      return { value: opt.value, label: opt.textContent };
    });
  }

  function parseMemberTeamIds(btn) {
    var raw = btn.getAttribute('data-member-team-ids');
    if (!raw) return {};
    try {
      var arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return {};
      var set = {};
      arr.forEach(function (id) { set[String(id)] = true; });
      return set;
    } catch (_e) {
      return {};
    }
  }

  function openAddDialog(btn) {
    if (!addDialog || !addSelect) return;
    pendingAddUserId = btn.getAttribute('data-user-id');
    if (!pendingAddUserId) return;
    var username = btn.getAttribute('data-username') || '';
    if (addUsername) addUsername.textContent = username;
    showError(addError, '');

    var joined = parseMemberTeamIds(btn);
    while (addSelect.firstChild) addSelect.removeChild(addSelect.firstChild);

    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '— выберите команду —';
    addSelect.appendChild(placeholder);

    var available = 0;
    allTeamOptions.forEach(function (o) {
      if (!o.value || joined[o.value]) return;
      var opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.label;
      addSelect.appendChild(opt);
      available += 1;
    });

    var has = available > 0;
    if (addField) addField.hidden = !has;
    if (addEmpty) addEmpty.hidden = has;
    if (addGo) addGo.disabled = !has;
    addSelect.value = '';

    openDialog(addDialog);
    if (has) {
      try { addSelect.focus(); } catch (_e) { /* игнор */ }
    }
  }

  document.addEventListener('click', function (event) {
    var btn = event.target.closest && event.target.closest('[data-admin-add-team]');
    if (!btn) return;
    openAddDialog(btn);
  });

  if (addCancel) {
    addCancel.addEventListener('click', function () { closeDialog(addDialog); });
  }

  if (addForm) {
    addForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!pendingAddUserId || !addSelect) return;
      showError(addError, '');
      var teamId = parseInt((addSelect.value || '').toString(), 10);
      if (!Number.isFinite(teamId) || teamId < 1) {
        showError(addError, 'Выберите команду.');
        return;
      }
      if (addGo) addGo.disabled = true;
      SMS.csrfFetch('/api/admin/users/' + encodeURIComponent(pendingAddUserId) + '/teams', {
        method: 'POST',
        body: { team_id: teamId }
      })
        .then(function (resp) {
          if (resp.ok) {
            reloadWithFlash('Пользователь добавлен в команду.', 'success');
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) {
            showError(addError, e.message);
            if (addGo) addGo.disabled = false;
          });
        })
        .catch(function () {
          showError(addError, 'Сетевая ошибка. Попробуйте ещё раз.');
          if (addGo) addGo.disabled = false;
        });
    });
  }

  /* ---- удаление доп. членства (делегирование submit) -------------------- */

  document.addEventListener('submit', function (event) {
    var form = event.target.closest && event.target.closest('[data-admin-remove-membership]');
    if (!form) return;
    event.preventDefault();

    var userId = form.getAttribute('data-user-id');
    var teamId = form.getAttribute('data-team-id');
    var username = form.getAttribute('data-username') || '';
    var teamName = form.getAttribute('data-team-name') || '';
    if (!userId || !teamId) return;

    var msg = 'Убрать пользователя ' + username + ' из команды «' + teamName +
      '»? Он перестанет видеть номера и получать SMS этой команды.';
    if (!window.confirm(msg)) return;

    var btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;

    var url = '/api/admin/users/' + encodeURIComponent(userId) +
      '/teams/' + encodeURIComponent(teamId);
    SMS.csrfFetch(url, { method: 'DELETE' })
      .then(function (resp) {
        if (resp.ok || resp.status === 204) {
          reloadWithFlash('Членство в команде удалено.', 'success');
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
})();
