/* Telegram Mini App SSO + адаптация темы — docs/05 §3, docs/08 §7.
 *
 * Грузится на каждой странице через base.html с ``defer`` (после официального
 * Telegram SDK, тоже ``defer``). ``defer`` гарантирует порядок: SDK, затем этот
 * glue-скрипт, оба после DOMContentLoaded.
 *
 * В обычном браузере ``window.Telegram`` не определён — скрипт делает
 * early-return (никаких мутаций DOM/CSS, никаких сетевых вызовов).
 *
 * Внутри Telegram WebView:
 *   - tg.ready()  -> Telegram прячет свой splash;
 *   - tg.expand() -> WebApp занимает всю высоту (mobile);
 *   - body получает класс ``tg-app`` (CSS переопределяет палитру на
 *     Telegram themeParams);
 *   - themeParams зеркалируются в CSS-переменные (--tg-bg, --tg-text и т.д.).
 *
 * Persistent SSO / self-heal (docs/05 §3): при непустом ``initData`` ВСЕГДА
 * POST-им его на ``/api/telegram/auth`` — и для анонимных, и для уже
 * залогиненных. Backend читает cookie ``sms_session`` и сам выбирает ветку:
 *   - нет сессии, привязка есть  -> {linked:true, redirect} + Set-Cookie;
 *   - нет сессии, привязки нет    -> {linked:false} + Set-Cookie sms_tg_pending;
 *   - есть сессия                 -> {linked:false, healed:true} (без redirect).
 *
 * Endpoint CSRF-exempt (защита — HMAC initData). Заголовок X-CSRF-Token не нужен.
 * initData НИКОГДА не логируется. ``__smsTgSsoTried`` защищает от повторных
 * вызовов (устанавливается ДО fetch — исключаем гонки/циклы).
 *
 * Никаких eval / innerHTML / document.write — только addEventListener,
 * classList.add и CSSStyleDeclaration.setProperty (CSP-безопасно).
 */
(function () {
  "use strict";

  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) {
    return;
  }

  if (typeof tg.ready === "function") {
    tg.ready();
  }
  if (typeof tg.expand === "function") {
    tg.expand();
  }

  document.body.classList.add("tg-app");

  var THEME_MAP = {
    bg_color: "--tg-bg",
    secondary_bg_color: "--tg-secondary-bg",
    text_color: "--tg-text",
    hint_color: "--tg-hint",
    link_color: "--tg-link",
    button_color: "--tg-button",
    button_text_color: "--tg-button-text",
  };

  function applyTheme() {
    var params = tg.themeParams || {};
    var root = document.documentElement;
    for (var key in THEME_MAP) {
      if (Object.prototype.hasOwnProperty.call(THEME_MAP, key)) {
        var value = params[key];
        if (value) {
          root.style.setProperty(THEME_MAP[key], value);
        }
      }
    }
  }

  applyTheme();

  if (typeof tg.onEvent === "function") {
    tg.onEvent("themeChanged", applyTheme);
  }

  var initData = typeof tg.initData === "string" ? tg.initData : "";

  if (initData && !window.__smsTgSsoTried) {
    window.__smsTgSsoTried = true;

    fetch("/api/telegram/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ init_data: initData }),
      credentials: "same-origin",
    })
      .then(function (response) {
        var status = response.status;
        return response
          .json()
          .then(function (body) {
            return { status: status, body: body };
          })
          .catch(function () {
            return { status: status, body: null };
          });
      })
      .then(function (result) {
        if (result.status !== 200 || !result.body) {
          // 401 (invalid_init_data / init_data_expired), 429, 5xx или битое
          // тело — молча деградируем, остаёмся на текущей странице.
          return;
        }
        var body = result.body;
        if (body.linked === true && body.redirect) {
          // Анонимный посетитель аутентифицирован по SSO: backend поставил
          // cookie sms_session/sms_csrf. Перезагружаемся в приложение.
          window.location.replace(body.redirect);
          return;
        }
        // body.linked === false:
        //   - анонимный без привязки: backend поставил sms_tg_pending,
        //     остаёмся на /login — обычный логин подхватит pending и создаст
        //     telegram_links при успехе;
        //   - self-heal залогиненного ({linked:false, healed:true}): без
        //     redirect и без cookie — НЕ перезагружаемся, страница как есть.
      })
      .catch(function () {
        // Сетевая ошибка (offline, CSP, DNS) — страница работает без SSO,
        // пользователь может войти вручную. Молчим.
      });
  }
})();
