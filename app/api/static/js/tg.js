/* Telegram Mini App SSO + адаптация темы + «залипающий» logout —
 * docs/05 §2/§3, docs/08 §2/§3/§7, ADR-0011.
 *
 * Грузится на каждой странице через base.html с ``defer`` (после официального
 * Telegram SDK, тоже ``defer``). ``defer`` гарантирует порядок: SDK, затем этот
 * glue-скрипт; оба выполняются после парсинга DOM (до DOMContentLoaded), поэтому
 * document.getElementById для элементов страницы уже доступен.
 *
 * ВАЖНО: официальный SDK (telegram-web-app.js) ВСЕГДА создаёт
 * ``window.Telegram.WebApp`` — и в реальном Mini App, и в обычном браузере.
 * Поэтому само наличие объекта НЕ является признаком Telegram-контекста
 * (иначе в браузере ложно навешивался бы класс ``tg-app`` и CSS прятал бы
 * верхнюю навигацию — см. main.css ``body.tg-app .topnav``). Браузерный стаб
 * SDK отдаёт ПУСТОЙ ``initData`` и ``platform === 'unknown'``; реальный Mini
 * App — непустой ``initData`` и/или конкретный ``platform`` (≠ 'unknown').
 * Детект ``inTelegram`` опирается именно на это. В обычном браузере
 * Telegram-специфичные действия (ready/expand/тема, класс ``tg-app``, авто-SSO)
 * пропускаются — навигация остаётся видимой. Скрипт при этом НЕ делает
 * early-return целиком: он ещё связывает кнопку «Войти» на /login (см. ниже),
 * что нужно и в браузере. Никаких мутаций DOM вне /login в браузере не будет —
 * элемент кнопки существует только в login.html.
 *
 * === Авто-SSO / self-heal (docs/05 §3, ADR-0004) ===
 * Внутри Telegram WebView при непустом ``initData`` скрипт POST-ит его на
 * ``/api/telegram/auth`` — и для анонимных, и для уже залогиненных. Backend
 * читает cookie ``sms_session`` и сам выбирает ветку:
 *   - нет сессии, привязка есть  -> {linked:true, redirect} + Set-Cookie;
 *   - нет сессии, привязки нет    -> {linked:false} + Set-Cookie sms_tg_pending;
 *   - есть сессия                 -> {linked:false, healed:true} (без redirect).
 *
 * === «Залипающий» logout (docs/05 §2/§3, docs/08 §2, ADR-0011) ===
 * После осознанного выхода backend ставит НЕ-HttpOnly cookie ``sms_logged_out=1``.
 * Пока маркер присутствует, авто-SSO ПОДАВЛЯЕТСЯ — initData НЕ отправляется
 * автоматически, иначе живая привязка (ADR-0004: logout её не трогает) мгновенно
 * перелогинила бы пользователя. Серверный fallback (docs/05 §3) на всякий случай
 * тоже откажет и вернёт {linked:false, logged_out:true} — на такой ответ мы НЕ
 * редиректим и остаёмся на /login.
 * Повторный вход — по явному клику кнопки «Войти» (§UX ADR-0011): клиент удаляет
 * cookie ``sms_logged_out`` (она не HttpOnly), затем инициирует вход:
 *   - в Mini App — POST initData на /api/telegram/auth (маркер уже снят →
 *     нормальное ветвление ADR-0004);
 *   - в браузере — просто фокус на форму шага-1 логина (она уже на странице).
 *
 * Endpoint /api/telegram/auth CSRF-exempt (защита — HMAC initData), X-CSRF-Token
 * не нужен. initData НИКОГДА не логируется. ``__smsTgSsoTried`` защищает от
 * повторных авто-вызовов (устанавливается ДО fetch — исключаем гонки/циклы).
 *
 * Никаких eval / innerHTML / document.write / inline-обработчиков — только
 * addEventListener, classList, hidden-атрибут, document.cookie и
 * CSSStyleDeclaration.setProperty (CSP-безопасно, docs/08 §7).
 */
(function () {
  "use strict";

  var LOGGED_OUT_COOKIE = "sms_logged_out";

  /** Прочитать cookie по имени. "" если нет. */
  function readCookie(name) {
    var target = name + "=";
    var parts = document.cookie ? document.cookie.split(";") : [];
    for (var i = 0; i < parts.length; i++) {
      var c = parts[i].trim();
      if (c.indexOf(target) === 0) {
        return decodeURIComponent(c.substring(target.length));
      }
    }
    return "";
  }

  /**
   * Удалить cookie ``sms_logged_out`` на клиенте (маркер не HttpOnly, ADR-0011).
   * Атрибуты Path=/ и (условно) Secure/SameSite=Lax повторяют серверные, чтобы
   * гарантированно совпасть с установленной cookie. Max-Age=0 — немедленное
   * удаление.
   */
  function clearLoggedOutMarker() {
    var secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie =
      LOGGED_OUT_COOKIE + "=; Path=/; Max-Age=0; SameSite=Lax" + secure;
  }

  var tg = window.Telegram && window.Telegram.WebApp;

  // Детект РЕАЛЬНОГО Mini App vs браузерного стаба SDK.
  // telegram-web-app.js создаёт ``window.Telegram.WebApp`` всегда, поэтому !!tg
  // недостаточно. В браузере стаб отдаёт пустой initData и platform==='unknown';
  // реальный Mini App — непустой initData и/или platform !== 'unknown'.
  // initData и platform вычисляем ДО inTelegram — от них зависит сам детект.
  var initData = tg && typeof tg.initData === "string" ? tg.initData : "";
  var tgPlatform =
    tg && typeof tg.platform === "string" ? tg.platform : "unknown";
  var inTelegram = !!tg && (initData !== "" || tgPlatform !== "unknown");

  // --- Telegram-специфичная инициализация (только в Mini App) ---------------
  if (inTelegram) {
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

    var applyTheme = function () {
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
    };

    applyTheme();

    if (typeof tg.onEvent === "function") {
      tg.onEvent("themeChanged", applyTheme);
    }
  }

  /**
   * POST initData на /api/telegram/auth и обработка ответа.
   * Guard ``__smsTgSsoTried`` защищает от повторов (авто + быстрый повторный
   * клик). Вызывается либо авто (если нет маркера), либо явно по кнопке «Войти».
   */
  function startSso() {
    if (!initData || window.__smsTgSsoTried) {
      return;
    }
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
          // Аутентифицирован по SSO: backend поставил sms_session/sms_csrf
          // (и очистил sms_logged_out). Перезагружаемся в приложение.
          window.location.replace(body.redirect);
          return;
        }
        // body.linked === false:
        //   - {logged_out:true} (ADR-0011 §3b): маркер выхода активен, сервер
        //     отказал в создании сессии — НЕ редиректим, остаёмся на /login;
        //   - анонимный без привязки: backend поставил sms_tg_pending,
        //     остаёмся на /login — обычный логин подхватит pending;
        //   - self-heal залогиненного ({healed:true}): без redirect/cookie —
        //     НЕ перезагружаемся, страница как есть.
        // Во всех случаях — без навигации.
      })
      .catch(function () {
        // Сетевая ошибка (offline, CSP, DNS) — страница работает без SSO,
        // пользователь может войти вручную. Молчим (initData не логируем).
      });
  }

  var loggedOut = readCookie(LOGGED_OUT_COOKIE) !== "";

  // --- Авто-SSO: только в Telegram, при initData и БЕЗ маркера выхода --------
  if (inTelegram && initData && !loggedOut) {
    startSso();
  }

  // --- Кнопка «Войти» на /login (ADR-0011 §5) -------------------------------
  // Элементы существуют только в login.html; на прочих страницах — no-op.
  var notice = document.getElementById("sms-logged-out-notice");
  var noticeText = document.getElementById("sms-logged-out-text");
  var loginButton = document.getElementById("sms-login-button");
  var usernameInput = document.getElementById("login-username");

  // Показываем блок явного входа, когда есть маркер выхода ИЛИ мы в Telegram
  // (в Telegram авто-SSO мог быть подавлён/не залогинить — даём явный вход).
  if (notice && (loggedOut || inTelegram)) {
    if (noticeText) {
      // Формулировка зависит от наличия маркера: вышедшему — «Вы вышли из
      // системы.», новому Mini App-пользователю (маркера нет) — нейтральный
      // призыв. textContent — без inline (CSP).
      noticeText.textContent = loggedOut
        ? "Вы вышли из системы."
        : "Войдите в систему через Telegram.";
    }
    notice.hidden = false;
  }

  if (loginButton) {
    loginButton.addEventListener("click", function () {
      // Явный вход: сначала снимаем «залипающий» маркер (ADR-0011 §4).
      clearLoggedOutMarker();

      if (inTelegram && initData) {
        // Mini App: маркер снят → нормальное ветвление ADR-0004. Разрешаем
        // повторную попытку (сбрасываем guard — это осознанное действие).
        window.__smsTgSsoTried = false;
        startSso();
        return;
      }
      // Обычный браузер: форма шага-1 уже на странице — просто ведём к ней.
      if (usernameInput && typeof usernameInput.focus === "function") {
        usernameInput.focus();
      }
    });
  }
})();
