/* =============================================================================
   app.js — landing участника/лидера (/app, docs/05 §7, ADR-0008).

   Backend (landing.py) отдаёт SSR-страницу с уже отрисованным списком номеров
   своей команды + статусом Telegram-привязки (SSR из контекста). Этот скрипт
   перехватывает управление списком: refresh после мутаций и обработка действий.

   Данные и мутации — через СУЩЕСТВУЮЩИЕ endpoints (нового API нет):
     - GET    /api/numbers            -> {numbers:[{id,phone_number,team_id,
                                          team_name,label,is_active,
                                          added_by_user_id,created_at}]}
     - POST   /api/numbers            {phone_number, label?}  (team_id — сервер
                                        берёт из current_user.team_id)
     - DELETE /api/numbers/{id}       -> {ok:true}

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

  /* Тексты ошибок для кодов, специфичных для /api/numbers (docs/05 §6).
     Общие коды (csrf_failed, not_authenticated, ...) знает SMS.errorText. */
  var NUMBER_ERRORS = {
    phone_number_taken: 'Этот номер уже привязан к команде.',
    invalid_phone_number: 'Некорректный номер. Формат: +71234567890 (E.164).',
    team_required: 'Не удалось определить команду. Обновите страницу и войдите заново.',
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
      text: 'Добавьте первый номер команды в форме ниже.'
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

  function renderList(numbers) {
    if (!numbers || numbers.length === 0) {
      renderEmpty();
      return;
    }
    // Сортировка по номеру для стабильного порядка.
    var sorted = numbers.slice().sort(function (a, b) {
      return (a.phone_number || '') < (b.phone_number || '') ? -1 : 1;
    });
    clear(root);
    var list = el('ul', { className: 'numbers-list', attrs: { 'data-numbers-list': '' } });
    sorted.forEach(function (n) { list.appendChild(buildCard(n)); });
    root.appendChild(list);
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
        renderList((data && Array.isArray(data.numbers)) ? data.numbers : []);
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
  var hasSsr = !!root.querySelector('[data-numbers-list], [data-numbers-empty]');
  refresh({ showLoading: !hasSsr });
})();
