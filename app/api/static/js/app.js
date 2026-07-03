/* =============================================================================
   app.js — landing участника/лидера (/app, docs/05 §7, ADR-0008, multi-team
   ADR-0012).

   Backend (landing.py) отдаёт SSR-страницу с номерами ВСЕХ своих команд,
   сгруппированными по командам, + статус Telegram-привязки. Этот скрипт
   перехватывает управление списком: refresh после мутаций, группировка,
   обработка действий и состояний.

   Данные и мутации — через СУЩЕСТВУЮЩИЕ endpoints (нового API нет):
     - GET    /api/numbers            -> {numbers:[{id,phone_number,team_id,
                                          team_name,label,is_active,
                                          added_by_user_id,created_at}]}
                                          (номера всех команд участника)
     - POST   /api/numbers            {phone_number, label?, team_id}
                                          (team_id — из селектора выбранной команды)
     - DELETE /api/numbers/{id}       -> {ok:true}

   Список команд для группировки/пустых групп — из data-teams (SSR app_teams).
   Все изменяющие запросы — через SMS.csrfFetch (double-submit X-CSRF-Token из
   cookie sms_csrf). Состояния корня: loading / error(retry) / empty / success.

   CSP-безопасно: DOM через createElement (без innerHTML пользовательских
   данных), события только через addEventListener. Секреты не логируются.
   ES2022, без сборки/транспиляции.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.SMS) return;

  var root = document.querySelector('[data-numbers-root]');
  if (!root) return;

  var SMS = window.SMS;

  /* Команды участника из SSR (data-teams='[{"id":..,"name":..}]') —
     для группировки номеров и отображения пустых команд. */
  var teams = parseTeams(root.getAttribute('data-teams'));

  function parseTeams(raw) {
    if (!raw) return [];
    try {
      var arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.filter(function (t) { return t && t.id != null; });
    } catch (_e) {
      return [];
    }
  }

  /* Тексты ошибок для кодов, специфичных для /api/numbers (docs/05 §6).
     Общие коды (csrf_failed, not_authenticated, ...) знает SMS.errorText. */
  var NUMBER_ERRORS = {
    phone_number_taken: 'Этот номер уже привязан к команде.',
    invalid_phone_number: 'Некорректный номер. Формат: +71234567890 (E.164).',
    team_required: 'Выберите команду для номера.',
    forbidden: 'Нет прав на управление номерами этой команды.',
    number_not_found: 'Номер не найден — возможно, он уже удалён.',
    team_not_found: 'Команда не найдена. Обновите страницу.'
  };

  function messageFor(err) {
    if (err && err.code && Object.prototype.hasOwnProperty.call(NUMBER_ERRORS, err.code)) {
      return NUMBER_ERRORS[err.code];
    }
    return (err && err.message) ? err.message : 'Запрос не выполнен.';
  }

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

  function byPhone(a, b) {
    return (a.phone_number || '') < (b.phone_number || '') ? -1 : 1;
  }

  /* ---- состояния корня --------------------------------------------------- */

  function renderLoading() {
    clear(root);
    root.appendChild(el('div', {
      className: 'admin__loading',
      text: 'Загрузка номеров…',
      attrs: { role: 'status' }
    }));
  }

  function renderError(message) {
    clear(root);
    var box = el('div', { className: 'admin__empty', attrs: { role: 'alert' } });
    box.appendChild(el('p', { text: message || 'Не удалось загрузить номера.' }));
    var retry = el('button', {
      className: 'btn btn--secondary',
      text: 'Повторить',
      attrs: { type: 'button' }
    });
    retry.addEventListener('click', function () { refresh({ showLoading: true }); });
    box.appendChild(retry);
    root.appendChild(box);
  }

  function renderEmpty() {
    clear(root);
    var box = el('div', { className: 'admin__empty', attrs: { role: 'status' } });
    box.appendChild(el('p', { text: 'Пока нет номеров.' }));
    box.appendChild(el('p', {
      className: 'field__hint',
      text: teams.length > 1
        ? 'Добавьте первый номер одной из ваших команд в форме ниже.'
        : 'Добавьте первый номер команды в форме ниже.'
    }));
    root.appendChild(box);
  }

  function buildCard(n) {
    var li = el('li', {
      className: 'number-card',
      attrs: { 'data-number-id': String(n.id) }
    });

    var main = el('div', { className: 'number-card__main' });
    main.appendChild(el('span', { className: 'number-card__phone', text: n.phone_number || '' }));
    if (n.label) {
      main.appendChild(el('span', { className: 'number-card__label', text: n.label }));
    }
    li.appendChild(main);

    var meta = el('div', { className: 'number-card__meta' });
    var active = n.is_active !== false;
    meta.appendChild(el('span', {
      className: 'admin-user__badge admin-user__badge--' + (active ? 'group_leader' : 'group_member'),
      text: active ? 'Активен' : 'Неактивен'
    }));

    var delBtn = el('button', {
      className: 'btn btn--danger btn--small',
      text: 'Удалить',
      attrs: {
        type: 'button',
        'data-number-delete': '',
        'data-number-id': String(n.id),
        'data-number-phone': n.phone_number || '',
        'aria-label': 'Удалить номер ' + (n.phone_number || '')
      }
    });
    meta.appendChild(delBtn);
    li.appendChild(meta);

    return li;
  }

  function buildGroup(team, nums) {
    var sec = el('section', {
      className: 'numbers-group',
      attrs: { 'data-numbers-group': '', 'data-team-id': String(team.id) }
    });
    sec.appendChild(el('h3', {
      className: 'numbers-group__title',
      text: team.name || ('Команда #' + team.id)
    }));
    if (!nums || nums.length === 0) {
      sec.appendChild(el('p', {
        className: 'numbers-group__empty field__hint',
        text: 'В этой команде пока нет номеров.'
      }));
    } else {
      var list = el('ul', { className: 'numbers-list' });
      nums.slice().sort(byPhone).forEach(function (n) { list.appendChild(buildCard(n)); });
      sec.appendChild(list);
    }
    return sec;
  }

  /* Рендер: группировка по командам (teams из SSR). Без teams — плоский список. */
  function renderNumbers(numbers) {
    if (!numbers || numbers.length === 0) {
      renderEmpty();
      return;
    }
    clear(root);

    if (teams.length === 0) {
      var flat = el('ul', { className: 'numbers-list', attrs: { 'data-numbers-list': '' } });
      numbers.slice().sort(byPhone).forEach(function (n) { flat.appendChild(buildCard(n)); });
      root.appendChild(flat);
      return;
    }

    var known = {};
    teams.forEach(function (t) {
      known[String(t.id)] = true;
      var tnums = numbers.filter(function (n) { return String(n.team_id) === String(t.id); });
      root.appendChild(buildGroup(t, tnums));
    });

    // Номера команд вне набора teams (edge-случай) — в отдельную группу.
    var orphans = numbers.filter(function (n) { return !known[String(n.team_id)]; });
    if (orphans.length) {
      root.appendChild(buildGroup({ id: 'other', name: 'Прочие команды' }, orphans));
    }
  }

  /* ---- загрузка / refresh ------------------------------------------------ */

  function refresh(opts) {
    opts = opts || {};
    if (opts.showLoading) renderLoading();
    return SMS.csrfFetch('/api/numbers', { method: 'GET' })
      .then(function (resp) {
        if (!resp.ok) {
          return SMS.readJsonError(resp).then(function (e) {
            throw new Error(messageFor(e));
          });
        }
        return resp.json();
      })
      .then(function (data) {
        renderNumbers((data && Array.isArray(data.numbers)) ? data.numbers : []);
      })
      .catch(function (e) {
        renderError(e && e.message ? e.message : 'Сетевая ошибка. Попробуйте ещё раз.');
      });
  }

  /* ---- удаление (делегирование на корне) --------------------------------- */

  root.addEventListener('click', function (event) {
    var btn = event.target.closest ? event.target.closest('[data-number-delete]') : null;
    if (!btn || !root.contains(btn)) return;
    event.preventDefault();

    var id = btn.getAttribute('data-number-id');
    var phone = btn.getAttribute('data-number-phone') || '';
    if (!id) return;
    if (!window.confirm('Удалить номер ' + phone + '? Действие необратимо.')) return;

    btn.disabled = true;
    SMS.csrfFetch('/api/numbers/' + encodeURIComponent(id), { method: 'DELETE' })
      .then(function (resp) {
        if (resp.ok) {
          SMS.flash('Номер удалён.', 'success');
          return refresh({ showLoading: false });
        }
        return SMS.readJsonError(resp).then(function (e) {
          SMS.flash(messageFor(e), 'error');
          btn.disabled = false;
        });
      })
      .catch(function () {
        SMS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
        btn.disabled = false;
      });
  });

  /* ---- добавление номера ------------------------------------------------- */

  var addForm = document.querySelector('[data-add-number-form]');
  var addError = document.querySelector('[data-add-error]');
  var addSubmit = document.querySelector('[data-add-submit]');
  var phoneInput = document.getElementById('add-phone');
  var labelInput = document.getElementById('add-label');
  var teamField = document.querySelector('[data-add-team]');

  function showAddError(text) {
    if (!addError) return;
    addError.textContent = text || '';
    addError.hidden = !text;
  }

  if (addForm) {
    addForm.addEventListener('submit', function (event) {
      event.preventDefault();
      showAddError('');

      var phone = (phoneInput && phoneInput.value ? phoneInput.value : '').trim();
      var label = (labelInput && labelInput.value ? labelInput.value : '').trim();

      if (!phone) { showAddError('Укажите номер телефона.'); return; }
      // Клиентская проверка E.164 (сервер валидирует повторно).
      if (!/^\+[1-9]\d{6,14}$/.test(phone)) {
        showAddError('Некорректный номер. Формат: +71234567890 (E.164).');
        return;
      }

      var payload = { phone_number: phone };
      payload.label = label ? label : null;

      // team_id — из селектора/скрытого поля (docs §7, §6). Без поля (edge) —
      // сервер применит домашнюю команду по умолчанию.
      if (teamField) {
        var teamRaw = (teamField.value || '').toString().trim();
        if (!teamRaw) { showAddError('Выберите команду для номера.'); return; }
        var teamId = parseInt(teamRaw, 10);
        if (!Number.isFinite(teamId) || teamId < 1) { showAddError('Выберите команду для номера.'); return; }
        payload.team_id = teamId;
      }

      if (addSubmit) addSubmit.disabled = true;
      SMS.csrfFetch('/api/numbers', { method: 'POST', body: payload })
        .then(function (resp) {
          if (resp.ok) {
            SMS.flash('Номер добавлен.', 'success');
            if (addForm) addForm.reset();
            return refresh({ showLoading: false });
          }
          return SMS.readJsonError(resp).then(function (e) { showAddError(messageFor(e)); });
        })
        .catch(function () { showAddError('Сетевая ошибка. Попробуйте ещё раз.'); })
        .then(function () { if (addSubmit) addSubmit.disabled = false; });
    });
  }

  /* ---- старт ------------------------------------------------------------- */

  // SSR уже отрисовал список: тихий refresh без loading-мелькания, чтобы
  // синхронизировать данные и перевесить кнопки на JS-рендер.
  var hasSsr = !!root.querySelector('[data-numbers-group], [data-numbers-list], [data-numbers-empty]');
  refresh({ showLoading: !hasSsr });
})();
