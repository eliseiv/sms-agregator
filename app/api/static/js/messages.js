/* =============================================================================
   messages.js — прогрессивное обогащение страницы /messages (docs/05 §9,
   ADR-0014). Просмотр входящих SMS, read-only.

   Первая страница рендерится server-side и работает БЕЗ JS (GET-форма фильтра +
   ссылка «Ещё» по cursor). Этот скрипт:
     - переформатирует время (<time datetime>) в локальную читаемую форму;
     - перехватывает «Ещё» → дозагружает следующую страницу через
         GET /api/messages?to_number&team_id&cursor&limit
       (SMS.csrfFetch) и дописывает сообщения БЕЗ перезагрузки и без дублей;
     - обновляет курсор/ссылку «Ещё» по next_cursor; убирает кнопку, когда
       next_cursor == null (forward-only).

   Состояния: loading (кнопка «Ещё» → «Загрузка…», aria-busy), error (флеш +
   возможность повторить), success (дозагруженные карточки), empty (страница
   без сообщений рендерится server-side; при пустой доп. странице «Ещё»
   просто убирается).

   Фильтр (номер/команда) — обычная GET-форма: submit перезагружает страницу
   server-side (работает без JS), поэтому JS его не перехватывает.

   CSP-safe: DOM только через createElement/textContent (никакого innerHTML с
   данными), данные — из <script type="application/json">, ошибки — через
   SMS.readJsonError/ERROR_MAP. Без сторонних зависимостей, ES2022.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.SMS) return;
  var SMS = window.SMS;

  var root = document.querySelector('[data-messages-root]');
  if (!root) return;

  var config = parseConfig(document.querySelector('[data-messages-config]'));
  var teamNames = buildTeamNames(config.teamNames);
  var labelByNumber = buildLabelMap(config.numberLabels);
  var nextCursor = config.nextCursor || null;
  var busy = false;

  var seen = collectSeenIds();

  // Переформатировать уже отрендеренные server-side отметки времени.
  reformatTimes(root);

  var moreLink = root.parentNode
    ? root.parentNode.querySelector('[data-messages-more]')
    : document.querySelector('[data-messages-more]');
  if (moreLink) {
    moreLink.addEventListener('click', onMoreClick);
  }

  /* ---- парсинг конфигурации ---------------------------------------------- */

  function parseConfig(node) {
    var fallback = { nextCursor: null, toNumber: '', teamId: null, isSuperAdmin: false, limit: 50, teamNames: [], numberLabels: [] };
    if (!node) return fallback;
    try {
      var data = JSON.parse(node.textContent || '{}');
      return {
        nextCursor: data.nextCursor || null,
        toNumber: data.toNumber || '',
        teamId: (data.teamId === 0 || data.teamId) ? data.teamId : null,
        isSuperAdmin: !!data.isSuperAdmin,
        limit: (typeof data.limit === 'number' && data.limit > 0) ? data.limit : 50,
        teamNames: Array.isArray(data.teamNames) ? data.teamNames : [],
        numberLabels: Array.isArray(data.numberLabels) ? data.numberLabels : []
      };
    } catch (_e) {
      return fallback;
    }
  }

  function buildTeamNames(pairs) {
    var map = {};
    (pairs || []).forEach(function (p) {
      if (p && p.id != null && p.name != null) map[String(p.id)] = p.name;
    });
    return map;
  }

  // Карта phone_number → label (эффективный лейбл = label or phone). null-label
  // означает «никнейма нет» (показываем сам номер).
  function buildLabelMap(pairs) {
    var map = {};
    (pairs || []).forEach(function (p) {
      if (p && p.num != null) map[String(p.num)] = (p.label != null && p.label !== '') ? p.label : null;
    });
    return map;
  }

  function collectSeenIds() {
    var set = {};
    var items = root.querySelectorAll('[data-message-id]');
    for (var i = 0; i < items.length; i++) {
      var id = items[i].getAttribute('data-message-id');
      if (id) set[id] = true;
    }
    return set;
  }

  /* ---- утилиты DOM ------------------------------------------------------- */

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

  function formatTime(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    try {
      return d.toLocaleString('ru-RU', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit'
      });
    } catch (_e) {
      return iso;
    }
  }

  function reformatTimes(scope) {
    var nodes = scope.querySelectorAll('time[data-message-time]');
    for (var i = 0; i < nodes.length; i++) {
      var iso = nodes[i].getAttribute('datetime');
      var text = formatTime(iso);
      if (text) nodes[i].textContent = text;
    }
  }

  /* ---- построение карточки сообщения ------------------------------------- */

  function buildCard(msg) {
    var li = el('li', { className: 'message-card', attrs: { 'data-message-id': String(msg.id) } });

    var head = el('div', { className: 'message-card__head' });
    head.appendChild(el('span', { className: 'message-card__from', text: msg.from_number || '' }));
    head.appendChild(el('span', { className: 'message-card__arrow', text: '→', attrs: { 'aria-hidden': 'true' } }));
    // Эффективный лейбл получателя: label or to_number (§6/§9).
    var toNum = msg.to_number || '';
    var lbl = labelByNumber[toNum];
    head.appendChild(el('span', { className: 'message-card__to', text: lbl ? lbl : toNum, attrs: { 'data-message-to': toNum } }));
    if (lbl) {
      head.appendChild(el('span', { className: 'message-card__to-raw', text: toNum, attrs: { 'data-message-to-raw': '' } }));
    }

    if (msg.team_id != null) {
      var name = teamNames[String(msg.team_id)] || ('Команда #' + msg.team_id);
      head.appendChild(el('span', { className: 'team-chip' }, [
        el('span', { className: 'team-chip__name', text: name })
      ]));
    }

    head.appendChild(el('time', {
      className: 'message-card__time',
      text: formatTime(msg.received_at),
      attrs: { datetime: msg.received_at || '', 'data-message-time': '' }
    }));
    li.appendChild(head);

    li.appendChild(el('p', { className: 'message-card__body', text: msg.body || '' }));
    return li;
  }

  function ensureList() {
    var list = root.querySelector('[data-messages-list]');
    if (list) return list;
    // Пустая страница была отрендерена как empty-state; заменяем на список.
    var empty = root.querySelector('[data-messages-empty]');
    if (empty && empty.parentNode) empty.parentNode.removeChild(empty);
    list = el('ul', { className: 'messages-list', attrs: { 'data-messages-list': '' } });
    root.appendChild(list);
    return list;
  }

  function appendMessages(messages) {
    if (!messages || !messages.length) return 0;
    var list = ensureList();
    var added = 0;
    messages.forEach(function (msg) {
      if (msg == null || msg.id == null) return;
      var key = String(msg.id);
      if (seen[key]) return; // защита от дублей
      seen[key] = true;
      list.appendChild(buildCard(msg));
      added += 1;
    });
    return added;
  }

  /* ---- построение URL следующей страницы --------------------------------- */

  function buildParams(cursor) {
    var params = new URLSearchParams();
    if (config.toNumber) params.set('to_number', config.toNumber);
    if (config.isSuperAdmin && config.teamId != null && config.teamId !== '') {
      params.set('team_id', String(config.teamId));
    }
    if (config.limit) params.set('limit', String(config.limit));
    if (cursor) params.set('cursor', cursor);
    return params;
  }

  function apiUrl(cursor) {
    return '/api/messages?' + buildParams(cursor).toString();
  }

  function pageUrl(cursor) {
    return '/messages?' + buildParams(cursor).toString();
  }

  /* ---- состояния кнопки «Ещё» -------------------------------------------- */

  function setMoreLoading() {
    if (!moreLink) return;
    moreLink.setAttribute('aria-busy', 'true');
    moreLink.setAttribute('aria-disabled', 'true');
    moreLink.textContent = 'Загрузка…';
  }

  function setMoreIdle() {
    if (!moreLink) return;
    moreLink.removeAttribute('aria-busy');
    moreLink.removeAttribute('aria-disabled');
    moreLink.textContent = 'Ещё';
  }

  function updateMore(cursor) {
    if (!moreLink) return;
    if (cursor) {
      moreLink.setAttribute('href', pageUrl(cursor));
      setMoreIdle();
    } else {
      removeMore();
    }
  }

  function removeMore() {
    if (!moreLink) return;
    var wrap = moreLink.closest ? moreLink.closest('[data-messages-more-wrap]') : moreLink.parentNode;
    if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
    moreLink = null;
  }

  /* ---- дозагрузка следующей страницы ------------------------------------- */

  function onMoreClick(event) {
    if (event) event.preventDefault();
    if (busy || !nextCursor) return;
    busy = true;
    setMoreLoading();

    SMS.csrfFetch(apiUrl(nextCursor), { method: 'GET' })
      .then(function (resp) {
        if (!resp.ok) {
          return SMS.readJsonError(resp).then(function (e) { throw new Error(e.message); });
        }
        return resp.json();
      })
      .then(function (data) {
        var messages = (data && Array.isArray(data.messages)) ? data.messages : [];
        appendMessages(messages);
        nextCursor = (data && data.next_cursor) ? data.next_cursor : null;
        updateMore(nextCursor);
        busy = false;
      })
      .catch(function (e) {
        busy = false;
        setMoreIdle();
        SMS.flash(e && e.message ? e.message : 'Не удалось загрузить ещё сообщения.', 'error');
      });
  }

  /* ---- редактирование никнейма номера (docs/05 §6/§9) -------------------- */

  // Обновить эффективный лейбл получателя во всех отрендеренных карточках для
  // данного номера (без перезагрузки).
  function applyLabelToCards(num, label) {
    if (!num) return;
    var spans = root.querySelectorAll('[data-message-to]');
    for (var i = 0; i < spans.length; i++) {
      var span = spans[i];
      if (span.getAttribute('data-message-to') !== num) continue;
      var raw = span.nextElementSibling;
      var hasRaw = !!(raw && raw.getAttribute && raw.getAttribute('data-message-to-raw') != null
        && raw.classList && raw.classList.contains('message-card__to-raw'));
      if (label) {
        span.textContent = label;
        if (hasRaw) {
          raw.textContent = num;
        } else {
          var r = el('span', { className: 'message-card__to-raw', text: num, attrs: { 'data-message-to-raw': '' } });
          span.parentNode.insertBefore(r, span.nextSibling);
        }
      } else {
        span.textContent = num;
        if (hasRaw && raw.parentNode) raw.parentNode.removeChild(raw);
      }
    }
  }

  // Обновить подпись опции в фильтре номера (phone — label).
  function updateFilterOption(num, label) {
    var sel = document.getElementById('filter-number');
    if (!sel || !num) return;
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value !== num) continue;
      sel.options[i].textContent = label ? (num + ' — ' + label) : num;
    }
  }

  document.addEventListener('submit', function (event) {
    var form = event.target.closest && event.target.closest('[data-number-nick-form]');
    if (!form) return;
    event.preventDefault();
    var id = form.getAttribute('data-number-id');
    var input = form.querySelector('[data-number-nick-input]');
    var save = form.querySelector('[data-number-nick-save]');
    if (!id || !input) return;

    var value = (input.value != null) ? input.value : '';
    if (save) save.disabled = true;

    SMS.csrfFetch('/api/numbers/' + encodeURIComponent(id), { method: 'PATCH', body: { label: value } })
      .then(function (resp) {
        if (!resp.ok) return SMS.readJsonError(resp).then(function (e) { throw new Error(e.message); });
        return resp.json();
      })
      .then(function (data) {
        var newLabel = (data && data.label) ? data.label : '';
        var phone = (data && data.phone_number) ? data.phone_number : (form.getAttribute('data-phone') || '');
        input.value = newLabel;
        if (phone) {
          labelByNumber[phone] = newLabel || null;
          applyLabelToCards(phone, newLabel);
          updateFilterOption(phone, newLabel);
        }
        SMS.flash(newLabel ? 'Никнейм сохранён.' : 'Никнейм удалён.', 'success');
        if (save) save.disabled = false;
      })
      .catch(function (e) {
        if (save) save.disabled = false;
        SMS.flash(e && e.message ? e.message : 'Не удалось сохранить никнейм.', 'error');
      });
  });
})();
