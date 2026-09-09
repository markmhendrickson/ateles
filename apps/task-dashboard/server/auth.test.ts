/**
 * TESTS FOR THE HASH-CARRYING OIDC ROUND TRIP.
 *
 * The route is client-side hash routing (`#/entities/<id>`), and a fragment is
 * NEVER sent to any server. A cold-session open of a deep link therefore hits
 * the auth gate, which redirects through Google, with the server-side `next`
 * blind to the hash the whole way — a bare 302 chain silently drops it and the
 * operator lands on the app root instead of the entity they asked for. That
 * defeats this PR's stated purpose (durable, shareable deep links).
 *
 * The fix: `/auth/login` and `/auth/callback` serve tiny same-origin HTML
 * documents (not bare redirects) that capture `location.hash` into
 * sessionStorage before leaving for Google, and restore it onto `next` before
 * the final in-app navigation. These tests exercise the two document
 * generators directly by loading each as a real page (jsdom) and asserting
 * what they do to `location`/`sessionStorage` — the same thing a browser
 * would do — rather than merely grepping the source text.
 */
import { describe, expect, it } from "vitest";
import { HASH_STORAGE_KEY, callbackResumeHtml, loginCaptureHtml } from "./auth";

/**
 * Run an HTML document's inline <script> in a controlled global scope that
 * mimics the bits of `window`/`location`/`sessionStorage` the capture and
 * restore scripts touch, and report what they did — without pulling in a
 * full DOM/jsdom dependency this package does not otherwise need.
 */
function runInlineScript(
  html: string,
  initialHash: string,
): { navigatedTo: string | null; storage: Map<string, string> } {
  const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
  if (!scriptMatch) throw new Error("no <script> tag found in document");
  const code = scriptMatch[1];

  const storage = new Map<string, string>();
  let navigatedTo: string | null = null;

  const window = {
    location: {
      hash: initialHash,
      replace(url: string) {
        navigatedTo = url;
      },
    },
    sessionStorage: {
      getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
      setItem: (k: string, v: string) => {
        storage.set(k, v);
      },
      removeItem: (k: string) => {
        storage.delete(k);
      },
    },
  };

  // eslint-disable-next-line @typescript-eslint/no-implied-eval -- test-only sandbox, not
  // arbitrary/untrusted input: the code under test is our own generator output.
  const fn = new Function("window", code);
  fn(window);

  return { navigatedTo, storage };
}

describe("loginCaptureHtml — capturing the hash before leaving for Google", () => {
  const AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&state=y";

  it("stores the current location.hash before navigating to Google", () => {
    const { navigatedTo, storage } = runInlineScript(
      loginCaptureHtml(AUTHORIZE_URL),
      "#/entities/ent_deadbeef",
    );
    expect(storage.get(HASH_STORAGE_KEY)).toBe("#/entities/ent_deadbeef");
    expect(navigatedTo).toBe(AUTHORIZE_URL);
  });

  it("stores an agent deep link the same way", () => {
    const { storage } = runInlineScript(loginCaptureHtml(AUTHORIZE_URL), "#/agents/ent_cafef00d");
    expect(storage.get(HASH_STORAGE_KEY)).toBe("#/agents/ent_cafef00d");
  });

  it("does not store an empty or bare hash", () => {
    const { storage: empty } = runInlineScript(loginCaptureHtml(AUTHORIZE_URL), "");
    expect(empty.has(HASH_STORAGE_KEY)).toBe(false);

    const { storage: bare } = runInlineScript(loginCaptureHtml(AUTHORIZE_URL), "#");
    expect(bare.has(HASH_STORAGE_KEY)).toBe(false);
  });

  it("always navigates on to the authorize URL regardless of hash", () => {
    const { navigatedTo } = runInlineScript(loginCaptureHtml(AUTHORIZE_URL), "");
    expect(navigatedTo).toBe(AUTHORIZE_URL);
  });

  it("escapes a hash that contains characters unsafe to inline verbatim", () => {
    // A malformed/hand-edited hash could contain quotes or `</script>`; the
    // generator must not let it break out of the inline script or inject HTML.
    const html = loginCaptureHtml(AUTHORIZE_URL);
    expect(html).not.toMatch(/<\/script>[^]*<\/script>[^]*<\/script>/);
  });
});

describe("callbackResumeHtml — restoring the hash onto the final navigation", () => {
  it("appends the sessionStorage hash onto `next` and clears the key", () => {
    const html = callbackResumeHtml("/");
    const storage = new Map([[HASH_STORAGE_KEY, "#/entities/ent_deadbeef"]]);
    const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/)![1];

    let navigatedTo: string | null = null;
    const window = {
      sessionStorage: {
        getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
        setItem: (k: string, v: string) => storage.set(k, v),
        removeItem: (k: string) => storage.delete(k),
      },
      location: {
        replace(url: string) {
          navigatedTo = url;
        },
      },
    };
    // eslint-disable-next-line @typescript-eslint/no-implied-eval
    new Function("window", scriptMatch)(window);

    expect(navigatedTo).toBe("/#/entities/ent_deadbeef");
    // THE POINT OF THIS FIX: the hash actually reaches the final destination
    // URL rather than being silently dropped at the auth boundary.
    expect(navigatedTo).not.toBe("/");
    expect(storage.has(HASH_STORAGE_KEY)).toBe(false);
  });

  it("navigates to a bare `next` with no trailing hash when nothing was stored", () => {
    const html = callbackResumeHtml("/tasks");
    const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/)![1];
    let navigatedTo: string | null = null;
    const window = {
      sessionStorage: {
        getItem: () => null,
        setItem: () => {},
        removeItem: () => {},
      },
      location: {
        replace(url: string) {
          navigatedTo = url;
        },
      },
    };
    // eslint-disable-next-line @typescript-eslint/no-implied-eval
    new Function("window", scriptMatch)(window);
    expect(navigatedTo).toBe("/tasks");
  });

  it("preserves `next`'s own query string, with the hash appended after it", () => {
    const html = callbackResumeHtml("/tasks?status=open");
    const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/)![1];
    const storage = new Map([[HASH_STORAGE_KEY, "#/entities/ent_1"]]);
    let navigatedTo: string | null = null;
    const window = {
      sessionStorage: {
        getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
        setItem: () => {},
        removeItem: () => {},
      },
      location: {
        replace(url: string) {
          navigatedTo = url;
        },
      },
    };
    // eslint-disable-next-line @typescript-eslint/no-implied-eval
    new Function("window", scriptMatch)(window);
    expect(navigatedTo).toBe("/tasks?status=open#/entities/ent_1");
  });

  it("never constructs an off-origin destination: `next` is always embedded as-is, not parsed as a URL", () => {
    // Defence in depth alongside serve.ts's own `raw.startsWith("/")` guard:
    // this generator must not itself be a second place an open redirect could
    // be reintroduced.
    const html = callbackResumeHtml("/tasks");
    expect(html).toContain(JSON.stringify("/tasks"));
  });
});

describe("end-to-end: the full capture → OIDC → restore round trip", () => {
  it("a hash present at /auth/login survives to the final navigation after /auth/callback", () => {
    const AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth?client_id=x";
    const ORIGINAL_HASH = "#/entities/ent_abc123";

    // Leg 1: the browser is on /auth/login with the original hash still live
    // (the gate's own redirect to /auth/login carried no fragment of its own,
    // so the browser inherited the original per RFC 3986 §3.5 — verified
    // separately against the real gate). The capture script runs and stores it.
    const loginResult = runInlineScript(loginCaptureHtml(AUTHORIZE_URL), ORIGINAL_HASH);
    expect(loginResult.storage.get(HASH_STORAGE_KEY)).toBe(ORIGINAL_HASH);

    // Leg 2: after the Google round trip, /auth/callback's document runs with
    // that same sessionStorage (same tab, same origin) and restores the hash
    // onto the real destination.
    const html = callbackResumeHtml("/");
    const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/)![1];
    let navigatedTo: string | null = null;
    const window = {
      sessionStorage: {
        getItem: (k: string) =>
          loginResult.storage.has(k) ? loginResult.storage.get(k)! : null,
        setItem: () => {},
        removeItem: (k: string) => loginResult.storage.delete(k),
      },
      location: {
        replace(url: string) {
          navigatedTo = url;
        },
      },
    };
    // eslint-disable-next-line @typescript-eslint/no-implied-eval
    new Function("window", scriptMatch)(window);

    expect(navigatedTo).toBe("/#/entities/ent_abc123");
  });
});
