/* =============================================================================
   admin_memberships.js — управление командами пользователя на /admin
   («+»-меню, docs/05 §4, §7; multi-team ADR-0012; паритет ADR-0015).

   Кнопка «+» (data-admin-menu-trigger, класс admin-users-table__add-group) в
   ячейке имени открывает меню-диалог (data-admin-actions-dialog) с двумя
   пунктами:
     - «Переместить в другую команду» → PATCH /api/admin/users/{id} {team_id}
       (сменa ДОМАШНЕЙ команды; задизейблено для лидера — backend тоже 409).
     - «Добавить в другую команду»    → POST  /api/admin/users/{id}/teams {team_id}
       (доп. членство; список команд исключает уже занятые — data-member-gids).

   Удаление доп. членства — «×» на чипе (data-admin-remove-membership): форма с
   no-JS fallback POST на ТОТ ЖЕ путь ресурса /api/admin/users/{id}/teams/{team_id}
   + _method=DELETE (docs §4; MethodOverride whitelist ^/api/admin/users/\d+/teams/\d+$),
   перехватывается → confirm + DELETE через SMS.csrfFetch.

   Контекст «+» захватывается при клике (data-user-id/-username/-current-gid/
   -is-leader/-member-gids) и разделяется диалогами перевода и добавления.

   После успеха — reload (SSR-группировка — единственный источник порядка);
   флеш переносится через sessionStorage (ключ общий с admin_users.js).

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

  function parseGidList(raw) {
    if (!raw) return [];
    try {
      var arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.map(function (n) { return parseInt(n, 10); })
                .filter(function (n) { return Number.isFinite(n) && n > 0; });
    } catch (_e) {
      return [];
    }
  }

  /* ---- контекст «+» (общий для перевода и добавления) ------------------- */

  var menuUserId = 0;
  var menuUsername = '';
  var menuCurrentGid = 0;   // домашняя команда пользователя
  var menuMemberGids = [];  // все команды пользователя
  var menuIsLeader = false;
  var menuDisplayName = '';  // текущее отображаемое имя (для префилла)

  /* ---- меню-диалог действий --------------------------------------------- */

  var actionsDialog = document.querySelector('[data-admin-actions-dialog]');
  var actionsUsername = document.querySelector('[data-admin-actions-username]');
  var actionsMoveBtn = document.querySelector('[data-admin-actions-move]');
  var actionsMoveDisabled = document.querySelector('[data-admin-actions-move-disabled]');
  var actionsAddBtn = document.querySelector('[data-admin-actions-add]');
  var actionsRenameBtn = document.querySelector('[data-admin-actions-rename]');

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest && event.target.closest('[data-admin-menu-trigger]');
    if (!trigger || !actionsDialog) return;
    menuUserId = parseInt(trigger.getAttribute('data-user-id') || '0', 10);
    menuUsername = trigger.getAttribute('data-username') || '';
    menuCurrentGid = parseInt(trigger.getAttribute('data-current-gid') || '0', 10);
    menuMemberGids = parseGidList(trigger.getAttribute('data-member-gids'));
    menuIsLeader = trigger.getAttribute('data-is-leader') === '1';
    menuDisplayName = trigger.getAttribute('data-display-name') || '';
    if (!menuUserId) return;

    if (actionsUsername) actionsUsername.textContent = menuUsername;
    // «Переместить» недоступно лидеру.
    if (actionsMoveBtn) {
      actionsMoveBtn.disabled = menuIsLeader;
      actionsMoveBtn.hidden = menuIsLeader;
    }
    if (actionsMoveDisabled) actionsMoveDisabled.hidden = !menuIsLeader;

    openDialog(actionsDialog);
    var firstAction = (actionsMoveBtn && !actionsMoveBtn.hidden) ? actionsMoveBtn : actionsAddBtn;
    if (firstAction) { try { firstAction.focus(); } catch (_e) { /* игнор */ } }
  });

  if (actionsMoveBtn) {
    actionsMoveBtn.addEventListener('click', function () {
      if (menuIsLeader) return;
      closeDialog(actionsDialog);
      openMoveDialog();
    });
  }
  if (actionsAddBtn) {
    actionsAddBtn.addEventListener('click', function () {
      closeDialog(actionsDialog);
      openAddDialog();
    });
  }
  if (actionsRenameBtn) {
    actionsRenameBtn.addEventListener('click', function () {
      closeDialog(actionsDialog);
      openRenameDialog();
    });
  }

  /* ---- перевод в другую команду (PATCH /api/admin/users/{id}) ----------- */

  var moveDialog = document.querySelector('[data-admin-move-dialog]');
  var moveForm = document.querySelector('[data-admin-move-form]');
  var moveSelect = document.querySelector('[data-admin-move-select]');
  var moveUsername = document.querySelector('[data-admin-move-username]');
  var moveError = document.querySelector('[data-admin-move-error]');
  var moveCancel = document.querySelector('[data-admin-move-cancel]');
  var moveGo = document.querySelector('[data-admin-move-go]');

  function openMoveDialog() {
    if (!moveDialog || !moveSelect || !menuUserId) return;
    if (moveUsername) moveUsername.textContent = menuUsername;
    showError(moveError, '');
    // Предвыбрать текущую домашнюю команду, чтобы админ видел исходное состояние.
    if (menuCurrentGid) moveSelect.value = String(menuCurrentGid);
    else moveSelect.selectedIndex = 0;
    openDialog(moveDialog);
  }

  if (moveCancel) moveCancel.addEventListener('click', function () { closeDialog(moveDialog); });

  if (moveForm) {
    moveForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!menuUserId || !moveSelect) return;
      showError(moveError, '');
      var teamId = parseInt((moveSelect.value || '').toString(), 10);
      if (!Number.isFinite(teamId) || teamId < 1) { showError(moveError, 'Выберите команду.'); return; }
      if (teamId === menuCurrentGid) { showError(moveError, 'Пользователь уже в этой команде.'); return; }
      if (moveGo) moveGo.disabled = true;
      SMS.csrfFetch('/api/admin/users/' + encodeURIComponent(menuUserId), { method: 'PATCH', body: { team_id: teamId } })
        .then(function (resp) {
          if (resp.ok) {
            reloadWithFlash('Пользователь переведён в другую команду.', 'success');
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) {
            showError(moveError, e.message);
            if (moveGo) moveGo.disabled = false;
          });
        })
        .catch(function () {
          showError(moveError, 'Сетевая ошибка. Попробуйте ещё раз.');
          if (moveGo) moveGo.disabled = false;
        });
    });
  }

  /* ---- добавление доп. членства (POST /api/admin/users/{id}/teams) ------ */

  var addDialog = document.querySelector('[data-admin-add-dialog]');
  var addForm = document.querySelector('[data-admin-add-form]');
  var addSelect = document.querySelector('[data-admin-add-select]');
  var addField = document.querySelector('[data-admin-add-field]');
  var addUsername = document.querySelector('[data-admin-add-username]');
  var addEmpty = document.querySelector('[data-admin-add-empty]');
  var addCancel = document.querySelector('[data-admin-add-cancel]');
  var addGo = document.querySelector('[data-admin-add-go]');
  var addError = document.querySelector('[data-admin-add-error]');

  // Полный набор опций команд захватываем один раз — на каждое открытие
  // пересобираем select, исключая команды, где пользователь уже состоит.
  var allTeamOptions = [];
  if (addSelect) {
    allTeamOptions = Array.prototype.slice.call(addSelect.options).map(function (opt) {
      return { value: opt.value, label: opt.textContent };
    });
  }

  function openAddDialog() {
    if (!addDialog || !addSelect || !menuUserId) return;
    if (addUsername) addUsername.textContent = menuUsername;
    showError(addError, '');

    var joined = {};
    menuMemberGids.forEach(function (g) { joined[String(g)] = true; });
    while (addSelect.firstChild) addSelect.removeChild(addSelect.firstChild);

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
    if (has) addSelect.selectedIndex = 0;

    openDialog(addDialog);
    if (has) { try { addSelect.focus(); } catch (_e) { /* игнор */ } }
  }

  if (addCancel) addCancel.addEventListener('click', function () { closeDialog(addDialog); });

  if (addForm) {
    addForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!menuUserId || !addSelect) return;
      showError(addError, '');
      var teamId = parseInt((addSelect.value || '').toString(), 10);
      if (!Number.isFinite(teamId) || teamId < 1) { showError(addError, 'Выберите команду.'); return; }
      if (addGo) addGo.disabled = true;
      SMS.csrfFetch('/api/admin/users/' + encodeURIComponent(menuUserId) + '/teams', {
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

  /* ---- смена отображаемого имени (PATCH /api/admin/users/{id}) ---------- */

  var renameDialog = document.querySelector('[data-admin-rename-dialog]');
  var renameForm = document.querySelector('[data-admin-rename-form]');
  var renameInput = document.querySelector('[data-admin-rename-input]');
  var renameUsername = document.querySelector('[data-admin-rename-username]');
  var renameError = document.querySelector('[data-admin-rename-error]');
  var renameCancel = document.querySelector('[data-admin-rename-cancel]');
  var renameGo = document.querySelector('[data-admin-rename-go]');

  function openRenameDialog() {
    if (!renameDialog || !renameInput || !menuUserId) return;
    if (renameUsername) renameUsername.textContent = menuUsername;
    showError(renameError, '');
    renameInput.value = menuDisplayName;
    openDialog(renameDialog);
    try { renameInput.focus(); } catch (_e) { /* игнор */ }
  }

  if (renameCancel) {
    renameCancel.addEventListener('click', function () { closeDialog(renameDialog); });
  }

  if (renameForm) {
    renameForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!menuUserId || !renameInput) return;
      showError(renameError, '');
      var value = (renameInput.value != null ? renameInput.value : '').trim();
      // Пустое имя → null (показывать логин); иначе — новое отображаемое имя.
      var payload = { display_name: value === '' ? null : value };
      if (renameGo) renameGo.disabled = true;
      SMS.csrfFetch('/api/admin/users/' + encodeURIComponent(menuUserId), { method: 'PATCH', body: payload })
        .then(function (resp) {
          if (resp.ok) {
            reloadWithFlash('Имя пользователя изменено.', 'success');
            return null;
          }
          return SMS.readJsonError(resp).then(function (e) {
            showError(renameError, e.message);
            if (renameGo) renameGo.disabled = false;
          });
        })
        .catch(function () {
          showError(renameError, 'Сетевая ошибка. Попробуйте ещё раз.');
          if (renameGo) renameGo.disabled = false;
        });
    });
  }

  /* ---- удаление доп. членства (делегирование submit «×») ---------------- */

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
