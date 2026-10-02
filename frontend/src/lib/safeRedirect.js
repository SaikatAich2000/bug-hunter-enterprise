/** Post-login `next` target, restricted to a same-origin path.
 *
 * A prefix regex is not enough: browsers strip tab/newline characters while
 * parsing, so "/\t/evil.example" survives a `^/(?![/\\])` check and then
 * navigates to "//evil.example". Resolving with the URL parser and comparing
 * origins judges the string exactly as the browser will.
 */
export function safeNextPath(requested, origin) {
  if (typeof requested !== "string" || !requested.startsWith("/")) return "/";
  let url;
  try {
    url = new URL(requested, origin);
  } catch {
    return "/";
  }
  if (url.origin !== origin) return "/";
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Where to go after a successful login.
 *
 * An explicit ?next= wins. Otherwise a fragment the login page received is kept:
 * opening an emailed "/#bug=5" while signed out is redirected server-side to
 * /login.html, and browsers carry the fragment across that redirect.
 */
export function loginTarget(search, hash, origin) {
  const next = new URLSearchParams(search).get("next");
  return safeNextPath(next || `/${hash || ""}`, origin);
}
