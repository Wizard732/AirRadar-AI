// Місток до нативних можливостей Android-додатка (Capacitor). На сайті/в Telegram/iPhone нічого не робить.
// Пуші БЕЗ Firebase: додаток генерує код, слухає ntfy-тему ar-<код> у фоновій службі; користувач надсилає боту /pair <код>.
(function () {
  'use strict';
  var cap = window.Capacitor;
  var native = !!(cap && cap.isNativePlatform && cap.isNativePlatform());
  var api = { native: native, code: '', status: 'web', platform: native ? cap.getPlatform() : 'web', battery: null };
  window.AirRadarNative = api;
  if (!native) return;

  function gen() {
    var a = 'abcdefghijklmnopqrstuvwxyz0123456789', r = crypto.getRandomValues(new Uint8Array(16)), s = '';
    for (var i = 0; i < r.length; i++) s += a[r[i] % 36];
    return s;
  }
  var code = '';
  try { code = localStorage.getItem('ar_code') || ''; } catch (e) {}
  if (!/^[a-z0-9]{12,32}$/.test(code)) { code = gen(); try { localStorage.setItem('ar_code', code); } catch (e) {} }
  api.code = code;
  function P() { return cap.Plugins && cap.Plugins.AirRadarPush; }

  api.init = async function () {
    if (!P()) { api.status = 'no-plugin'; return api.status; }
    try { var r = await P().start({ topic: 'ar-' + api.code }); api.status = r.status || 'ready'; }
    catch (e) { api.status = 'error'; api.error = String(e && e.message || e); }
    try { api.battery = (await P().batteryExemption({ request: false })).ignoring; } catch (e) {}
    return api.status;
  };
  api.newCode = async function () {
    if (P()) { try { await P().stop(); } catch (e) {} }
    api.code = gen(); try { localStorage.setItem('ar_code', api.code); } catch (e) {}
    return api.init();
  };
  api.test = function () { return P() && P().test(); };
  api.askBattery = async function () { if (P()) { await P().batteryExemption({ request: true }); } };
  api.ready = api.init();
})();
