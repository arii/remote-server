export const DEFAULT_ORIGIN = "https://dev-onasafari.ddns.net:8443";

export function serverOrigin(value) {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("Enter an HTTPS origin only, without a path or credentials.");
  }
  return url.origin;
}

export function socketUrl(origin) {
  return `wss://${new URL(serverOrigin(origin)).host}/buttons/api/browser/ext`;
}
