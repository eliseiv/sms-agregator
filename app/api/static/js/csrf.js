/* =============================================================================
   csrf.js — универсальные хелперы для CSRF-безопасного AJAX. Грузится base.html.

   docs/08-security §3 (double-submit):
     - cookie ``sms_csrf`` ставит backend (не HttpOnly — JS может читать);
     - для изменяющих запросов JS шлёт заголовок ``X-CSRF-Token`` с тем же
       значением, что и cookie;
     - backend сверяет заголовок/поле с токеном сессии.

   Экспортирует ``window.SMS``:
     - getCsrfToken(): string
     - csrfFetch(url, options): Promise<Response>
     - flash(text, category)
     - readJsonError(response): Promise<{code, message}>  (flat-envelope
       docs/05: {"error": code, "detail": msg})
     - errorText(code, fallback): string

   Плюс: на загрузке дозаполняет пустые скрытые поля ``input[name=csrf_token]``
   значением из cookie ``sms_csrf`` — нужно для logout-формы и no-JS-fallback
   форм админки, которым backend не передаёт csrf_token в контексте.

   Без сторонних зависимостей. ES2022, без транспиляции.
   ========================================================================== */
(function () {
  'use strict';

  var CSRF_COOKIE = 'sms_csrf';

  /* Человекочитаемые тексты для кодов ошибок docs/05. */
  var ERROR_MAP = {
    not_authenticated: 'Требуется авторизация. Войдите заново.',
    forbidden: 'Доступ запрещён.',
    csrf_failed: 'Ошибка безопасности (CSRF). Перезагрузите страницу и повторите.',
    validation_error: 'Проверьте правильность заполнения формы.',
    rate_limited: 'Слишком много запросов. Попробуйте позже.',
    not_found: 'Запись не найдена.',
    conflict: 'Такая запись уже существует.',
    internal_error: 'Внутренняя ошибка сервера. Попробуйте позже.',
    method_override_not_allowed: 'Запрос отклонён.',
    // users
    team_required: 'Команда обязательна — выберите команду.',
    username_taken: 'Такой логин уже занят.',
    invalid_username: 'Недопустимый логин (3–64 символа: латиница, цифры, _ . -).',
    team_not_found: 'Команда не найдена.',
    user_not_found: 'Пользователь не найден.',
    user_is_leader: 'Пользователь — лидер команды с участниками. Сначала назначьте другого лидера.',
    leader_move_forbidden: 'Лидера нельзя перевести, пока в команде есть участники. Сначала назначьте другого лидера.',
    cannot_delete_super_admin: 'Администратора удалить нельзя.',
    cannot_reset_super_admin: 'Пароль администратора сбросить нельзя.',
    role_team_invariant: 'Недопустимое сочетание роли и команды.',
    // membership (multi-team, ADR-0012, docs/05 §4)
    cannot_add_super_admin_to_team: 'Администратора нельзя добавить в команду.',
    membership_already_exists: 'Пользователь уже состоит в этой команде.',
    cannot_remove_home_membership: 'Нельзя убрать домашнюю команду. Смените её через «Перевести».',
    membership_not_found: 'Членство не найдено — возможно, уже удалено.',
    // teams
    team_name_taken: 'Команда с таким названием уже есть.',
    invalid_name: 'Недопустимое название (1–100 символов).',
    team_has_members: 'В команде есть участники — сначала удалите/переведите их.',
    user_not_in_team: 'Кандидат не является участником этой команды.',
    // numbers (docs/05 §4a)
    number_not_found: 'Номер не найден.',
    invalid_query: 'Недопустимая комбинация фильтров.'
  };

  function errorText(code, fallback) {
    if (code && Object.prototype.hasOwnProperty.call(ERROR_MAP, code)) {
      return ERROR_MAP[code];
    }
    return fallback || 'Запрос не выполнен.';
  }

  /** Прочитать cookie по имени. "" если нет. */
  function readCookie(name) {
    var target = name + '=';
    var parts = document.cookie ? document.cookie.split(';') : [];
    for (var i = 0; i < parts.length; i++) {
      var c = parts[i].trim();
      if (c.indexOf(target) === 0) {
        return decodeURIComponent(c.substring(target.length));
      }
    }
    return '';
  }

  function getCsrfToken() {
    var cookieValue = readCookie(CSRF_COOKIE);
    if (cookieValue) return cookieValue;
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }

  /**
   * fetch-обёртка:
   *   - всегда шлёт cookies (credentials: 'same-origin'),
   *   - для изменяющих методов добавляет X-CSRF-Token,
   *   - Accept: application/json по умолчанию,
   *   - Content-Type: application/json, если body — обычный объект.
   */
  function csrfFetch(url, options) {
    var opts = Object.assign({}, options || {});
    var method = (opts.method || 'GET').toUpperCase();
    var headers = new Headers(opts.headers || {});

    if (!headers.has('Accept')) {
      headers.set('Accept', 'application/json');
    }

    if (
      opts.body &&
      typeof opts.body === 'object' &&
      !(opts.body instanceof FormData) &&
      !(opts.body instanceof URLSearchParams) &&
      !(opts.body instanceof Blob) &&
      typeof opts.body.byteLength !== 'number'
    ) {
      opts.body = JSON.stringify(opts.body);
      if (!headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
      }
    }

    var isStateChanging = method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS';
    if (isStateChanging) {
      var token = getCsrfToken();
      if (token) headers.set('X-CSRF-Token', token);
    }

    opts.headers = headers;
    opts.credentials = opts.credentials || 'same-origin';
    return fetch(url, opts);
  }

  /**
   * Разобрать flat-envelope ошибки docs/05: {"error": code, "detail": msg}.
   * Возвращает {code, message}. Fallback — по HTTP-статусу.
   */
  async function readJsonError(response) {
    var data = null;
    try {
      data = await response.json();
    } catch (_e) {
      data = null;
    }
    if (data && typeof data.error === 'string') {
      return {
        code: data.error,
        message: errorText(data.error, data.detail || null)
      };
    }
    return {
      code: 'http_' + response.status,
      message: 'Запрос не выполнен (HTTP ' + response.status + ').'
    };
  }

  /** Транзиентное flash-сообщение вверху <main>. */
  function flash(text, category) {
    var cat = category || 'info';
    var main = document.getElementById('main') || document.querySelector('main');
    if (!main) return;
    var list = main.querySelector('.flashes');
    if (!list) {
      list = document.createElement('ul');
      list.className = 'flashes';
      list.setAttribute('role', 'status');
      list.setAttribute('aria-live', 'polite');
      main.insertBefore(list, main.firstChild);
    }
    var item = document.createElement('li');
    item.className = 'flash flash--' + cat;
    item.textContent = text;
    list.appendChild(item);
    if (cat === 'success' || cat === 'info') {
      setTimeout(function () {
        if (item.parentNode) item.parentNode.removeChild(item);
      }, 6000);
    }
  }

  /** Дозаполнить пустые скрытые csrf_token-поля из cookie (logout, no-JS формы). */
  function populateCsrfInputs() {
    var token = getCsrfToken();
    if (!token) return;
    var inputs = document.querySelectorAll('input[name="csrf_token"]');
    for (var i = 0; i < inputs.length; i++) {
      if (!inputs[i].value) inputs[i].value = token;
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', populateCsrfInputs);
  } else {
    populateCsrfInputs();
  }

  window.SMS = Object.freeze({
    getCsrfToken: getCsrfToken,
    csrfFetch: csrfFetch,
    readJsonError: readJsonError,
    errorText: errorText,
    flash: flash
  });
})();
