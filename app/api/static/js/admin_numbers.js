/* =============================================================================
   admin_numbers.js — секция «Нераспределённые номера» на /admin (docs/05 §4a, §7).

   Первичный рендер unassigned-номеров — серверный (SSR-инжект
   ``unassigned_numbers``). Этот скрипт улучшает секцию:
     - переключатель фильтра assignment (unassigned | assigned | all):
         GET /api/admin/numbers?assignment=<X>
     - назначение команды номеру:
         PATCH /api/admin/numbers/{id}  {team_id: <int>}
     - снятие команды (возврат в unassigned-пул):
         PATCH /api/admin/numbers/{id}  {team_id: null}

   Список команд для select берётся из data-teams (SSR ``teams | tojson``),
   без дополнительного запроса. Все мутации — через SMS.csrfFetch (double-submit).

   Состояния контейнера [data-numbers-root]: loading / error(retry) / empty /
   success. CSP-безопасно: DOM только через createElement/textContent, события —
   через addEventListener/делегирование, без innerHTML пользовательских данных.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.SMS) return;
  var SMS = window.SMS;

  var section = document.querySelector('[data-numbers-section]');
  if (!section) return;
  var root = section.querySelector('[data-numbers-root]');
  if (!root) return;

  var filterSelect = section.querySelector('[data-numbers-filter]');

  // Текущий фильтр совпадает с SSR-первичным рендером (unassigned).
  var currentAssignment = (filterSelect && filterSelect.value) || 'unassigned';

  // Команды из SSR (data-teams='[{"id":..,"name":..}]').
  var teams = parseTeams();

  function parseTeams() {
    var raw = section.getAttribute('data-teams');
    if (!raw) return [];
    try {
      var arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.filter(function (t) { return t && t.id != null; });
    } catch (_e) {
      return [];
    }
  }

  /* ---- утилиты DOM ------------------------------------------------------- */

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

  function teamNameById(id) {
    for (var i = 0; i < teams.length; i++) {
      if (String(teams[i].id) === String(id)) return teams[i].name;
    }
    return null;
  }

  /* ---- состояния корня --------------------------------------------------- */

  function renderLoading() {
    clear(root);
    root.appendChild(el('div', { className: 'admin__loading', text: 'Загрузка номеров…', attrs: { role: 'status' } }));
  }

  function renderError(message) {
    clear(root);
    var box = el('div', { className: 'admin__empty admin__empty--inline', attrs: { role: 'alert' } });
    box.appendChild(el('p', { text: message || 'Не удалось загрузить номера.' }));
    var retry = el('button', { className: 'btn btn--secondary btn--small', text: 'Повторить', attrs: { type: 'button' } });
    retry.addEventListener('click', function () { loadNumbers(currentAssignment); });
    box.appendChild(retry);
    root.appendChild(box);
  }

  function renderEmpty(assignment) {
    clear(root);
    var box = el('div', { className: 'admin__empty admin__empty--inline', attrs: { role: 'status' } });
    if (assignment === 'assigned') {
      box.appendChild(el('p', { text: 'Распределённых номеров нет.' }));
    } else if (assignment === 'all') {
      box.appendChild(el('p', { text: 'Номеров пока нет.' }));
    } else {
      box.appendChild(el('p', { text: 'Нераспределённых номеров нет.' }));
      box.appendChild(el('p', { className: 'field__hint', text: 'Все номера привязаны к командам. Переключите фильтр, чтобы увидеть распределённые.' }));
    }
    root.appendChild(box);
  }

  /* ---- построение строки номера ----------------------------------------- */

  function buildTeamSelect(numberId) {
    var select = el('select', {
      className: 'field__input field__input--compact',
      attrs: { id: 'assign-team-' + numberId, name: 'team_id', required: 'required' }
    });
    select.setAttribute('data-number-team-select', '');
    select.appendChild(el('option', { text: '— выберите команду —', attrs: { value: '' } }));
    teams.forEach(function (t) {
      select.appendChild(el('option', { text: t.name, attrs: { value: String(t.id) } }));
    });
    return select;
  }

  function buildAssignForm(n) {
    var form = el('form', {
      className: 'inline-form number-card__assign',
      attrs: { method: 'POST', action: '/api/admin/numbers/' + n.id }
    });
    form.setAttribute('data-number-assign-form', '');
    form.setAttribute('data-number-id', String(n.id));

    var mo = el('input', { attrs: { type: 'hidden', name: '_method', value: 'PATCH' } });
    form.appendChild(mo);

    var label = el('label', { className: 'visually-hidden', text: 'Команда для номера ' + n.phone_number, attrs: { for: 'assign-team-' + n.id } });
    form.appendChild(label);

    if (teams.length === 0) {
      form.appendChild(el('span', { className: 'admin-users-table__muted', text: 'Сначала создайте команду.' }));
      return form;
    }

    form.appendChild(buildTeamSelect(n.id));
    var go = el('button', { className: 'btn btn--primary btn--small', text: 'Назначить', attrs: { type: 'submit' } });
    go.setAttribute('data-number-assign-go', '');
    form.appendChild(go);
    return form;
  }

  function buildAssignedControls(n) {
    var wrap = el('div', { className: 'number-card__meta' });
    var tname = n.team_name || teamNameById(n.team_id) || ('команда #' + n.team_id);
    wrap.appendChild(el('span', { className: 'team-chip team-chip--home' }, [el('span', { className: 'team-chip__name', text: tname })]));
    var unassign = el('button', { className: 'btn btn--secondary btn--small', text: 'Снять', attrs: { type: 'button' } });
    unassign.setAttribute('data-number-unassign', '');
    unassign.setAttribute('data-number-id', String(n.id));
    unassign.setAttribute('data-phone', n.phone_number || '');
    wrap.appendChild(unassign);
    return wrap;
  }

  function buildNumberCard(n) {
    var li = el('li', { className: 'number-card', attrs: { 'data-number-id': String(n.id) } });

    var main = el('div', { className: 'number-card__main' });
    main.appendChild(el('span', { className: 'number-card__phone', text: n.phone_number || '' }));
    if (n.label) main.appendChild(el('span', { className: 'number-card__label', text: n.label }));
    if (n.is_active === false) {
      main.appendChild(el('span', { className: 'admin-user__badge admin-user__badge--group_member', text: 'неактивен' }));
    }
    li.appendChild(main);

    if (n.team_id == null) {
      li.appendChild(buildAssignForm(n));
    } else {
      li.appendChild(buildAssignedControls(n));
    }
    return li;
  }

  function renderList(numbers, assignment) {
    if (!numbers || numbers.length === 0) { renderEmpty(assignment); return; }
    clear(root);
    var list = el('ul', { className: 'numbers-list' });
    numbers.forEach(function (n) { list.appendChild(buildNumberCard(n)); });
    root.appendChild(list);
  }

  /* ---- загрузка ---------------------------------------------------------- */

  function loadNumbers(assignment) {
    currentAssignment = assignment;
    renderLoading();
    var url = '/api/admin/numbers?assignment=' + encodeURIComponent(assignment);
    SMS.csrfFetch(url, { method: 'GET' })
      .then(function (resp) {
        if (!resp.ok) return SMS.readJsonError(resp).then(function (e) { throw new Error(e.message); });
        return resp.json();
      })
      .then(function (data) {
        var numbers = (data && Array.isArray(data.numbers)) ? data.numbers : [];
        renderList(numbers, assignment);
      })
      .catch(function (e) {
        renderError(e && e.message ? e.message : 'Сетевая ошибка.');
      });
  }

  /* ---- фильтр ------------------------------------------------------------ */

  if (filterSelect) {
    filterSelect.addEventListener('change', function () {
      loadNumbers(filterSelect.value || 'unassigned');
    });
  }

  /* ---- назначение команды (делегирование submit) ------------------------ */

  function patchTeam(numberId, teamId, okText, onErr) {
    return SMS.csrfFetch('/api/admin/numbers/' + encodeURIComponent(numberId), {
      method: 'PATCH',
      body: { team_id: teamId }
    }).then(function (resp) {
      if (resp.ok) {
        SMS.flash(okText, 'success');
        loadNumbers(currentAssignment);
        return null;
      }
      return SMS.readJsonError(resp).then(function (e) {
        if (onErr) onErr(e.message); else SMS.flash(e.message, 'error');
      });
    }).catch(function () {
      if (onErr) onErr('Сетевая ошибка. Попробуйте ещё раз.'); else SMS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
    });
  }

  root.addEventListener('submit', function (event) {
    var form = event.target.closest && event.target.closest('[data-number-assign-form]');
    if (!form) return;
    event.preventDefault();
    var numberId = form.getAttribute('data-number-id');
    var select = form.querySelector('[data-number-team-select]');
    var go = form.querySelector('[data-number-assign-go]');
    if (!numberId || !select) return;
    var teamId = parseInt((select.value || '').toString(), 10);
    if (!Number.isFinite(teamId) || teamId < 1) { SMS.flash('Выберите команду.', 'error'); return; }
    if (go) go.disabled = true;
    patchTeam(numberId, teamId, 'Номер назначен команде.').then(function () {
      if (go) go.disabled = false;
    });
  });

  /* ---- снятие команды (делегирование click) ----------------------------- */

  root.addEventListener('click', function (event) {
    var btn = event.target.closest && event.target.closest('[data-number-unassign]');
    if (!btn) return;
    var numberId = btn.getAttribute('data-number-id');
    var phone = btn.getAttribute('data-phone') || '';
    if (!numberId) return;
    if (!window.confirm('Снять команду у номера ' + phone + '? Номер вернётся в нераспределённый пул, доставки по нему прекратятся.')) return;
    btn.disabled = true;
    patchTeam(numberId, null, 'Команда снята — номер в нераспределённом пуле.').then(function () {
      btn.disabled = false;
    });
  });
})();
