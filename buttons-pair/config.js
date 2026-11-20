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

// Pairing token (#1409): the pairing route skips OAuth at the proxy
// (an extension WebSocket is cross-site and cannot carry the cookie),
// so the extension proves pairing with a shared token. Browsers cannot
// set arbitrary WebSocket headers, so it rides as a
// Sec-WebSocket-Protocol subprotocol. Subprotocols must be HTTP token
// characters, hence the format check.
export const PAIR_TOKEN_PATTERN = /^[A-Za-z0-9._~-]{16,}$/;

export function pairProtocols(token) {
  if (!token) return [];
  if (!PAIR_TOKEN_PATTERN.test(token)) {
    throw new Error("Pairing token must be at least 16 letters, digits, or ._~- characters.");
  }
  return [`pair.${token}`];
}
