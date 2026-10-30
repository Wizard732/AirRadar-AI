// Збирає www/ для Capacitor із ../miniapp: app.html → index.html (оболонка з вкладками), radar.html, native.js, іконки.
// Дані (tracks/stats/impacts.json) додаток бере з GitHub Pages — адресу задає AIRRADAR_DATA_BASE.
import { cpSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = join(root, '..', 'miniapp');
const out = join(root, 'www');
const DATA_BASE = process.env.AIRRADAR_DATA_BASE || 'https://wizard732.github.io/AirRadar-AI/';

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
const inject = `<script>window.AIRRADAR_DATA_BASE=${JSON.stringify(DATA_BASE)};</script>`;
for (const [from, to] of [['app.html', 'index.html'], ['radar.html', 'radar.html']]) {
  const html = readFileSync(join(src, from), 'utf8').replace('<head>', '<head>\n' + inject);
  writeFileSync(join(out, to), html);
}
for (const f of ['native.js', 'icon-192.png', 'icon-512.png', 'raions.json']) cpSync(join(src, f), join(out, f));
console.log('www готовий →', out, '| дані з', DATA_BASE);
