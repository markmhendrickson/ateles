/**
 * Google sign-in gate for the DEPLOYED dashboard.
 *
 * WHY THIS FILE IS THE PRECONDITION FOR DEPLOYING AT ALL
 * -----------------------------------------------------
 * On localhost the dashboard needed no auth: the only way to reach it was to
 * be sitting at the machine. Deployed on Fly it is reachable by anyone who
 * learns the URL, and every route behind it reads the operator's Neotoma graph
 * with a full-privilege bearer token. Without this gate, deploying would
 * publish the entire task graph — every task, session digest, agent report and
 * open question — to the open internet. That is strictly worse than the outage
 * this deployment exists to fix, so the gate is not a follow-up: no request
 * reaches an `/api/*` route or a built asset until it carries a valid session
 * cookie issued here.
 *
 * WHY GOOGLE SSO RATHER THAN A SHARED TOKEN OR FLY'S OWN CONTROLS
 * --------------------------------------------------------------
 * Three options were assessed:
 *
 *   1. A shared secret in the URL or a password prompt. Rejected: it is a
 *      bearer credential that lands in browser history, gets pasted into
 *      chats alongside the task links this deployment exists to make
 *      shareable, and cannot be revoked per-person. There is nothing to
 *      revoke except the one secret everybody holds.
 *
 *   2. Fly's platform access controls. Rejected: Fly has no per-app end-user
 *      authentication. Its access control governs who can ADMINISTER the app
 *      (deploy, read secrets) via org membership, not who may load a page it
 *      serves. Putting the org boundary in front of HTTP traffic would mean
 *      giving a viewer deploy rights — a strictly larger grant than intended.
 *
 *   3. Google OIDC against an email allowlist. CHOSEN. The operator already
 *      signs into Neotoma this way; the mechanism, the Google OAuth client and
 *      the allowlist concept are all established rather than invented here.
 *      Access is per-person and revocable by removing one address, no shared
 *      secret exists to leak, and there is no new vendor.
 *
 * This module deliberately mirrors `neotoma/src/services/google_oidc.ts`
 * rather than importing it — the two repos share no package boundary. The
 * security-relevant decisions are kept identical on purpose: verify the
 * id_token against Google's live JWKS (signature, issuer, audience, expiry),
 * REQUIRE `email_verified`, and only then check the allowlist. Verifying the
 * allowlist without verifying the signature would let anyone mint a token
 * naming an approved address.
 *
 * FAIL CLOSED. If the Google client id, secret, allowlist or session key is
 * missing, `requireAuth` rejects every request rather than passing them
 * through. A misconfigured deploy serves nobody, which is the safe direction:
 * the failure mode of the opposite choice is silently publishing the graph.
 */
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { createRemoteJWKSet, jwtVerify } from "jose";

const GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"];
const GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs";
const GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth";
const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";

/** How long a signed-in session lasts before Google must be consulted again. */
const SESSION_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
/** The out-and-back hop to Google. Short: it is a redirect, not a session. */
const STATE_TTL_MS = 10 * 60 * 1000;

export const SESSION_COOKIE = "ateles_dash_session";

let cachedJwks: ReturnType<typeof createRemoteJWKSet> | null = null;
function getJwks() {
  if (!cachedJwks) cachedJwks = createRemoteJWKSet(new URL(GOOGLE_JWKS_URL));
  return cachedJwks;
}

function env(name: string): string {
  return (process.env[name] || "").trim();
}

/**
 * The allowlist, as a set of normalized addresses.
 *
 * Case-insensitive because email is, in practice, case-insensitive — and
 * these are Google accounts, the only kind this flow will ever see.
 */
function approvedEmails(): Set<string> {
  const out = new Set<string>();
  for (const raw of env("DASHBOARD_APPROVED_EMAILS").split(",")) {
    const e = raw.trim().toLowerCase();
    if (e) out.add(e);
  }
  return out;
}

/**
 * Whether auth is fully configured. Every piece must be present: a client id
 * and secret to talk to Google, a non-empty allowlist to have anyone to admit,
 * and a session key to sign cookies with.
 */
export function authConfigured(): { ok: boolean; missing: string[] } {
  const missing: string[] = [];
  if (!env("DASHBOARD_GOOGLE_CLIENT_ID")) missing.push("DASHBOARD_GOOGLE_CLIENT_ID");
  if (!env("DASHBOARD_GOOGLE_CLIENT_SECRET")) missing.push("DASHBOARD_GOOGLE_CLIENT_SECRET");
  if (approvedEmails().size === 0) missing.push("DASHBOARD_APPROVED_EMAILS");
  if (!env("DASHBOARD_SESSION_KEY")) missing.push("DASHBOARD_SESSION_KEY");
  return { ok: missing.length === 0, missing };
}

// ── Signed session cookies ───────────────────────────────────────────────────
// A session is `<base64url payload>.<hmac>`. The payload carries the verified
// email and an absolute expiry. Nothing is stored server-side, so a restarted
// or replaced machine does not sign everybody out — which matters because this
// app is redeployed on every merge to main.

function sign(value: string): string {
  return createHmac("sha256", env("DASHBOARD_SESSION_KEY")).update(value).digest("base64url");
}

export function issueSession(email: string): string {
  const payload = Buffer.from(
    JSON.stringify({ email, exp: Date.now() + SESSION_TTL_MS }),
  ).toString("base64url");
  return `${payload}.${sign(payload)}`;
}

/**
 * Verify a session cookie and return the email it attests to, or null.
 *
 * The signature is compared with `timingSafeEqual`. Both halves are hex/base64
 * of a fixed-width digest, so a length mismatch is itself a rejection rather
 * than something to compare.
 */
export function readSession(cookie: string | undefined): string | null {
  if (!cookie) return null;
  const dot = cookie.lastIndexOf(".");
  if (dot <= 0) return null;

  const payload = cookie.slice(0, dot);
  const provided = Buffer.from(cookie.slice(dot + 1));
  const expected = Buffer.from(sign(payload));
  if (provided.length !== expected.length) return null;
  if (!timingSafeEqual(provided, expected)) return null;

  try {
    const claims = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    if (typeof claims.email !== "string") return null;
    if (typeof claims.exp !== "number" || claims.exp < Date.now()) return null;
    // Re-check the allowlist on every request, not only at sign-in: removing
    // an address must take effect immediately rather than at cookie expiry.
    if (!approvedEmails().has(claims.email)) return null;
    return claims.email;
  } catch {
    return null;
  }
}

export function parseCookies(header: string | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (header || "").split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    out[part.slice(0, eq).trim()] = decodeURIComponent(part.slice(eq + 1).trim());
  }
  return out;
}

// ── The Google redirect leg ─────────────────────────────────────────────────
// `state` is a single-use CSRF nonce bound to the path the user was trying to
// reach, held in memory. In-memory is correct here: it lives for 10 minutes,
// and a redeploy mid-sign-in costs one retry rather than a lost session.

const stateStore = new Map<string, { next: string; expiresAt: number }>();

export function createState(next: string): string {
  const now = Date.now();
  for (const [k, v] of stateStore) if (v.expiresAt <= now) stateStore.delete(k);
  const nonce = randomBytes(24).toString("base64url");
  stateStore.set(nonce, { next, expiresAt: now + STATE_TTL_MS });
  return nonce;
}

export function consumeState(nonce: string): { next: string } | null {
  const entry = stateStore.get(nonce);
  if (!entry) return null;
  stateStore.delete(nonce);
  if (entry.expiresAt <= Date.now()) return null;
  return { next: entry.next };
}

export function buildAuthorizeUrl(redirectUri: string, state: string): string {
  const url = new URL(GOOGLE_AUTHORIZE_URL);
  url.searchParams.set("client_id", env("DASHBOARD_GOOGLE_CLIENT_ID"));
  url.searchParams.set("redirect_uri", redirectUri);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("scope", "openid email");
  url.searchParams.set("state", state);
  // Avoid silently reusing a stale Google session belonging to a different
  // (possibly non-approved) account in a shared browser.
  url.searchParams.set("prompt", "select_account");
  return url.toString();
}

/**
 * Exchange the authorization code for an id_token, verify it against Google's
 * live JWKS, and return the verified email — but only if it is verified AND
 * on the allowlist. Throws otherwise; callers must not treat a throw as
 * anything but a rejection.
 */
export async function verifyGoogleCode(
  code: string,
  redirectUri: string,
): Promise<string> {
  const clientId = env("DASHBOARD_GOOGLE_CLIENT_ID");

  const res = await fetch(GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      code,
      client_id: clientId,
      client_secret: env("DASHBOARD_GOOGLE_CLIENT_SECRET"),
      redirect_uri: redirectUri,
      grant_type: "authorization_code",
    }).toString(),
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) throw new Error("Google token exchange failed");

  const json = (await res.json()) as { id_token?: string };
  if (!json.id_token) throw new Error("Google returned no id_token");

  // Signature, issuer, audience and expiry are all enforced here. Checking the
  // allowlist without this step would admit anyone able to craft a JWT naming
  // an approved address.
  const { payload } = await jwtVerify(json.id_token, getJwks(), {
    issuer: GOOGLE_ISSUERS,
    audience: clientId,
  });

  const email = typeof payload.email === "string" ? payload.email.trim().toLowerCase() : "";
  const verified = payload.email_verified === true || payload.email_verified === "true";
  if (!email || !verified) throw new Error("Google account has no verified email");
  if (!approvedEmails().has(email)) throw new Error("not approved");

  return email;
}
