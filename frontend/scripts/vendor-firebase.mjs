// Copies Firebase's compat UMD bundles into app/static/vendor so they can be
// self-hosted (CSP keeps script-src 'self' — no gstatic CDN). The FCM service
// worker (served at /firebase-messaging-sw.js) importScripts these. Runs as a
// build step so the vendored copies always match the installed firebase version.
import { copyFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, '..', 'node_modules', 'firebase');
const dest = resolve(here, '..', '..', 'app', 'static', 'vendor');

mkdirSync(dest, { recursive: true });
for (const file of ['firebase-app-compat.js', 'firebase-messaging-compat.js']) {
  copyFileSync(resolve(src, file), resolve(dest, file));
  console.log('vendored', file);
}
