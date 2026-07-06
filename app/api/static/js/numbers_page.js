/* =============================================================================
   numbers_page.js — прогрессивное обогащение страницы /numbers.

   Формы действий работают БЕЗ JS (POST + _method=PATCH/DELETE + csrf → JSON).
   Этот скрипт перехватывает submit, шлёт запрос через SMS.csrfFetch (CSRF-хедер)
   и перезагружает страницу, чтобы отразить изменение (никнейм/команда/удаление).

   Действия (data-number-action):
     - nick   → PATCH  /api/numbers/{id}        {label}     (никнейм; пусто = снять)
     - delete → DELETE /api/numbers/{id}                    (с подтверждением)
     - move   → PATCH  /api/admin/numbers/{id}  {team_id}   (перенос, super_admin)

   CSP-safe: делегированный listener, без inline-обработчиков. ES2022.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.SMS) return;
  var SMS = window.SMS;

  document.addEventListener('submit', function (event) {
    var form = event.target.closest && event.target.closest('[data-number-action]');
    if (!form) return;
    event.preventDefault();

    var action = form.getAttribute('data-number-action');
    var url = form.getAttribute('action');
    if (!url) return;

    var method = 'PATCH';
    var opts = { method: 'PATCH' };

    if (action === 'nick') {
      var input = form.querySelector('input[name="label"]');
      opts.body = { label: input ? (input.value != null ? input.value : '') : '' };
    } else if (action === 'move') {
      var sel = form.querySelector('select[name="team_id"]');
      var v = sel ? sel.value : '';
      opts.body = { team_id: v === '' ? null : Number(v) };
    } else if (action === 'delete') {
      var phone = form.getAttribute('data-phone') || 'этот номер';
      if (!window.confirm('Удалить номер ' + phone + '? Действие необратимо.')) return;
      method = 'DELETE';
      opts = { method: 'DELETE' };
    } else {
      return;
    }

    var btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;

    SMS.csrfFetch(url, opts)
      .then(function (resp) {
        if (!resp.ok) {
          return SMS.readJsonError(resp).then(function (e) { throw new Error(e.message); });
        }
        return resp.json().catch(function () { return {}; });
      })
      .then(function () {
        var msg = action === 'delete' ? 'Номер удалён.'
          : action === 'move' ? 'Номер перенесён.'
          : 'Никнейм сохранён.';
        SMS.flash(msg, 'success');
        window.location.reload();
      })
      .catch(function (e) {
        if (btn) btn.disabled = false;
        SMS.flash(e && e.message ? e.message : 'Не удалось выполнить действие.', 'error');
      });
  });
})();
