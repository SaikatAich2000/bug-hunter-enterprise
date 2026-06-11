# Changelog

All notable changes to Bug Hunter (Enterprise). Format roughly follows
[Keep a Changelog](https://keepachangelog.com/).

## [2.10] — 2026-06-11

**Maintenance + quality pass.** Schema migrations remain strictly
additive — zero DB changes.

- SonarQube cleanup: resolved all open issues and security hotspots;
  raised automated test coverage above the gate threshold.

## [2.8] — 2026-06-04

**Security hardening** — OWASP audit + remediation. Eight items, all
additive, no DB schema change.

- *Login timing equalised* (G1) — bcrypt runs even for unknown emails so
  response latency stops leaking account existence.
- *CSV export defanged* (G2) — cells starting with `=`/`+`/`-`/`@`/`\t`/`\r`
  prefixed with `'` to neutralise Excel formula injection.
- *Body-size middleware* (G3) — 60 MB cap (env-tunable via
  `MAX_REQUEST_BODY_BYTES`) rejects oversized requests before the body
  is read.
- *X-Forwarded-For trust gate* (G4) — `auth.py:_client_ip` now honours
  `TRUST_PROXY_FORWARDED_FOR`, matching the rate-limit middleware. No
  more XFF-spoofed audit IPs on direct deploys.
- *PII out of logs* (G5) — INFO log lines mask emails to `a***@domain`.
- *Per-account lockout* (T3) — 10 fails / 15 min triggers a 15-min 429.
  Email-keyed; counts unknown-email attempts too so the lock state
  can't be used to enumerate. Applies to the 2FA second-step as well so
  TOTP codes can't be brute-forced unchecked. Env tunables:
  `LOGIN_FAIL_LIMIT`, `LOGIN_FAIL_WINDOW_SECONDS`,
  `LOGIN_LOCKOUT_SECONDS`.
- *Breach-corpus check* (T4) — HaveIBeenPwned k-anonymity API rejects
  known-pwned passwords on every set-password path (signup,
  change-password, reset-password, admin create-user, admin password
  reset). Fail-open on network errors; off-switch via
  `PASSWORD_BREACH_CHECK_ENABLED=false`.
- *Image EXIF strip* (T6) — Pillow drops GPS / camera-serial / XMP /
  ICC from uploaded JPEG / PNG / GIF / WEBP / BMP / TIFF. Non-images
  pass through.

**Anti-enumeration** — inactive-account login now returns the same
unified 401 + identical detail as a wrong-password failure. Previously
returned `403 "Account is disabled"`, which let an attacker who knew a
valid password tell *exists but disabled* from *wrong password*.

**UI fixes** — `.auth-card-wide` actually renders wider (parent shell
no longer caps it); logout confirm dialog z-index now stacks above the
Sleuth FAB and sibling modals; mobile modals use `100dvh` instead of
`100vh` so the modal head isn't tucked under the iOS / Android browser
chrome; auth pages get safe-area-inset padding so the logo isn't hidden
behind the notch on phones with one.

**Tests** — +62 security regression tests; full suite **752 passing /
1 skipped** on SQLite + Postgres-compatible code paths.

## [2.7]

Sonar gate green; cognitive-complexity refactors; SPA modernisation;
+JS/Python test coverage; multi-tenant isolation hardened.

## [2.6]

Rich-text editor for descriptions + comments. Custom calendar /
dropdowns. Newest-first comments / attachments / tasks.

## [2.5]

Per-item-type status sets. Admin-curated comments / attachments.
Card-style controls bars across Events / Sessions / Audit.

## [2.4]

Audit history survives bug deletion (`activity_log.bug_id` becomes
`ON DELETE SET NULL`). Read-only mode for restricted users.

---

Older releases: see git history (`git log --oneline`).
