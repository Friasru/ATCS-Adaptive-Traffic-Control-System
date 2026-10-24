// Session helpers shared by the Dashboard and Monitor pages.
//
// The token lives in memory on each page and is mirrored to sessionStorage
// (this tab only), so navigating between pages or reloading keeps the user
// signed in. If storage is unavailable, memory still works.

const SESSION_KEY = "atcs.session";
// setTimeout fires at once for longer delays, so longer lifetimes are capped.
const MAX_TIMER_MS = 2147483647;

function saveStoredSession(token, username) {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token, username }));
  } catch (err) {
    // Storage blocked or full: the session stays in memory only.
  }
}

function clearStoredSession() {
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch (err) {
    // Nothing stored that we can reach.
  }
}

function readStoredSession() {
  try {
    const stored = JSON.parse(sessionStorage.getItem(SESSION_KEY));
    if (stored && typeof stored.token === "string" && stored.token &&
        typeof stored.username === "string" && stored.username) {
      return { token: stored.token, username: stored.username };
    }
  } catch (err) {
    // Storage unavailable or corrupt JSON.
  }
  return null;
}

// Milliseconds until the JWT's exp claim, or null if the token cannot be read.
// The signature is not checked here; the server answers a bad token with 401.
function tokenLifetime(token) {
  if (typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(b64.padEnd(Math.ceil(b64.length / 4) * 4, "=")));
    if (!payload || typeof payload.exp !== "number" || !Number.isFinite(payload.exp)) return null;
    return payload.exp * 1000 - Date.now();
  } catch (err) {
    return null;
  }
}

// Lifetime of a fresh login. The session never outlives the token: use
// whichever of expires_in and exp ends first.
function loginLifetime(accessToken, expiresIn) {
  const fromExp = tokenLifetime(accessToken);
  const fromServer = expiresIn * 1000;
  return fromExp === null ? fromServer : fromServer > 0 ? Math.min(fromServer, fromExp) : fromExp;
}
