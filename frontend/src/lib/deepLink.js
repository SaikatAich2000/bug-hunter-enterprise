/** Parse the notification deep-link hash ("#bug=12" / "#event=3").
 *
 * Emails, web push and desktop notifications all link to `/#bug=<id>` or
 * `/#event=<id>`. Only positive integer ids are accepted; anything else is
 * ignored so a crafted hash can't reach the API with an unexpected value.
 */
export function parseDeepLink(hash) {
  const m = /^#(bug|event)=([1-9]\d{0,9})$/.exec(hash || "");
  if (!m) return null;
  return { kind: m[1], id: Number(m[2]) };
}
