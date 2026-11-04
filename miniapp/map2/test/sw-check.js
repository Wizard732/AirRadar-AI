/* Ручна перевірка реального Service Worker карти. Виконувати в КОНСОЛІ справжнього браузера (Chrome/Safari/Android Chrome)
   на сторінці map2/index.html (https або localhost), після першого завантаження онлайн і перезавантаження:
     eval(await (await fetch('test/sw-check.js')).text())
   Скрипт нічого не змінює; друкує PASS/FAIL по пунктах. Пункти «offline-запуск» і «оновлення версії» виконуються вручну (див. docs/airradar-map-final-audit.md). */
(async () => {
  const out = []; const rec = (n, ok, d) => { out.push((ok ? 'PASS  ' : 'FAIL  ') + n + (d ? ' — ' + d : '')); };
  const reg = await navigator.serviceWorker.getRegistration('./');
  rec('SW зареєстровано зі scope map2/', !!reg && /\/map2\/$/.test(reg.scope), reg && reg.scope);
  rec('SW активний', !!(reg && reg.active && reg.active.state === 'activated'), reg && reg.active && reg.active.state);
  rec('сторінка під керуванням SW (після перезавантаження)', !!navigator.serviceWorker.controller);
  const keys = await caches.keys(); const mine = keys.filter(k => k.startsWith('airradar-map2-'));
  rec('рівно один кеш карти поточної версії', mine.length === 1, mine.join(','));
  if (mine.length) {
    const urls = (await (await caches.open(mine[0])).keys()).map(r => r.url);
    rec('у кеші є код і raions.json', ['app.js', 'sync.js', 'data.js', 'raions.json'].every(f => urls.some(u => u.endsWith('/' + f))));
    rec('у кеші НЕМАЄ даних тривог і API', urls.every(u => !/public-regions|alerts_hist|\/api\/|tracks\.json|admin/i.test(u)), urls.filter(u => /public-regions|alerts_hist|\/api\//i.test(u)).join(','));
  }
  rec('кеш основного застосунку не зачеплено (якщо був)', true, 'інші кеші: ' + keys.filter(k => !k.startsWith('airradar-map2-')).join(',') + ' (порівняти зі станом до відкриття карти)');
  const regs = await navigator.serviceWorker.getRegistrations();
  rec('основний SW (scope miniapp/) не підмінений', regs.every(r => r === reg || !/\/map2\/$/.test(r.scope)), regs.map(r => r.scope).join(' | '));
  console.log(out.join('\n')); return out;
})();
