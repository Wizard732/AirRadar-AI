// Місток до нативних можливостей мобільного додатка (Capacitor). На сайті/в Telegram нічого не робить.
// Пуші: додаток САМ генерує код, підписується на FCM-тему u_<код>, а користувач надсилає боту /pair <код>.
(function () {
  'use strict';
  var cap = window.Capacitor;
  var native = !!(cap && cap.isNativePlatform && cap.isNativePlatform());
  var api = { native: native, code: '', status: 'web', platform: native ? cap.getPlatform() : 'web', last: null };
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
  api.newCode = function () { var old = 'u_' + api.code; api.code = gen(); try { localStorage.setItem('ar_code', api.code); } catch (e) {} return api.init(old); };

  var CHANNELS = [
    { id: 'airradar_alarm', name: 'Тривога', description: 'Повітряна тривога та небезпека поруч', importance: 5, sound: 'alarm', vibration: true, lights: true, visibility: 1 },
    { id: 'airradar_warn', name: 'Попередження', description: 'Можливо до вашого міста', importance: 4, vibration: true, visibility: 1 },
    { id: 'airradar_info', name: 'Інформація', description: 'Відбій та інші повідомлення', importance: 3, visibility: 1 }
  ];

  api.init = async function (oldTopic) {
    var FM = cap.Plugins && cap.Plugins.FirebaseMessaging;
    if (!FM) { api.status = 'no-plugin'; return api.status; }
    try {
      if (oldTopic) { try { await FM.unsubscribeFromTopic({ topic: oldTopic }); } catch (e) {} }
      var perm = await FM.requestPermissions();
      if (perm.receive !== 'granted') { api.status = 'denied'; return api.status; }
      if (api.platform === 'android') { for (var i = 0; i < CHANNELS.length; i++) { try { await FM.createChannel(CHANNELS[i]); } catch (e) {} } }
      await FM.subscribeToTopic({ topic: 'u_' + api.code });
      api.status = 'ready';
    } catch (e) { api.status = 'error'; api.error = String(e && e.message || e); }
    return api.status;
  };

  var started = api.init();
  api.ready = started;
  try {
    var FM2 = cap.Plugins && cap.Plugins.FirebaseMessaging;
    if (FM2 && FM2.addListener) FM2.addListener('notificationReceived', function (ev) {
      api.last = ev && ev.notification; if (window.arOnPush) try { window.arOnPush(api.last); } catch (e) {}
    });
  } catch (e) {}
})();
