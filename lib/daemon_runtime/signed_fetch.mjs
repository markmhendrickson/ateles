#!/usr/bin/env node
/**
 * Per-agent AAuth signed fetch for the Ateles daemons.
 *
 * Python can't produce RFC 9421 HTTP Message Signatures, and the neotoma
 * `request` CLI silently falls back to UNSIGNED on any hiccup. So daemons shell
 * out to this helper, which calls the proven `cliSignedFetch` from the neotoma
 * RC build directly. The caller sets the per-agent identity via env:
 *   NEOTOMA_AAUTH_PRIVATE_JWK_PATH  (the agent's keys/<agent>.jwk.json)
 *   NEOTOMA_AAUTH_SUB / NEOTOMA_AAUTH_KID
 * and NEOTOMA_RC_DIR (default ~/neotoma-rc-src) to locate the signer.
 *
 * IO: a JSON request spec on stdin -> a JSON result on stdout.
 *   in:  { "url": "...", "method": "POST", "headers": {...}, "body": "..." }
 *   out: { "response_received": true, "status": 200 } followed by a newline,
 *        then { "status": 200, "ok": true, "body": "<response text>" }
 *        { "error": "<message>" }   (exit 1 before a response)
 *
 * Note: what a signed write may touch is decided server-side by the agent_grant
 * the signature is admitted against (its ops and entity types). The server's
 * NEOTOMA_STRICT_AAUTH_SUBS does not promote a tier; it refuses an unsigned
 * request whose X-Agent-Label names a listed sub. Tier promotion comes from
 * NEOTOMA_OPERATOR_ATTESTED_SUBS. New daemon writes use the in-process Python
 * signer (neotoma_signed.NeotomaWriter), not this helper.
 */

import { createHash } from "node:crypto";
import os from "node:os";
import path from "node:path";

function rcDir() {
  return process.env.NEOTOMA_RC_DIR || path.join(os.homedir(), "neotoma-rc-src");
}

async function loadSigner() {
  const base = rcDir();
  for (const rel of ["dist/cli/aauth_signer.js"]) {
    try {
      return await import(`file://${path.join(base, rel)}`);
    } catch {
      /* fall through */
    }
  }
  throw new Error(`cliSignedFetch not found under ${base}/dist/cli/aauth_signer.js`);
}

async function readStdin() {
  let s = "";
  for await (const chunk of process.stdin) s += chunk;
  return s;
}

// RFC 7638 thumbprint of an EC JWK's public members: the same value
// aauth_httpsig.jwk_thumbprint computes and the server records as agent_thumbprint.
function ecThumbprint(jwk) {
  if (!jwk || jwk.kty !== "EC") return null;
  const canonical = JSON.stringify({ crv: jwk.crv, kty: "EC", x: jwk.x, y: jwk.y });
  return createHash("sha256").update(canonical).digest("base64url");
}

async function main() {
  const spec = JSON.parse(await readStdin());
  if (!spec.url) throw new Error("request spec missing 'url'");
  const { cliSignedFetch, loadCliSignerConfig } = await loadSigner();
  // A caller that pinned its signing principal sends `expect_thumbprint`. The key is
  // then read ONCE into memory here, its thumbprint is compared, and that same
  // in-memory key is what signs (configOverride): the key file is not opened again by
  // pathname after the check, so it cannot change between the check and the signature.
  // Mismatch aborts before anything is signed or sent.
  let configOverride;
  if (spec.expect_thumbprint) {
    if (typeof loadCliSignerConfig !== "function") {
      throw new Error("signer cannot load the key it would sign with");
    }
    configOverride = await loadCliSignerConfig();
    if (!configOverride || ecThumbprint(configOverride.privateJwk) !== spec.expect_thumbprint) {
      process.stdout.write(JSON.stringify({ error: "identity_changed" }));
      process.exit(1);
    }
  }
  const resp = await cliSignedFetch(spec.url, {
    method: (spec.method || "GET").toUpperCase(),
    headers: spec.headers || {},
    body: spec.body,
    ...(configOverride ? { configOverride } : {}),
  });
  // Preserve the response boundary before reading a body that may stall or fail.
  process.stdout.write(JSON.stringify({ response_received: true, status: resp.status }) + "\n");
  let text;
  try {
    text = await resp.text();
  } catch {
    process.stdout.write(JSON.stringify({ status: resp.status, ok: resp.ok, body_error: true }));
    process.exitCode = 1;
    return;
  }
  process.stdout.write(JSON.stringify({ status: resp.status, ok: resp.ok, body: text }));
}

main().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String(err && err.message ? err.message : err) }));
  process.exit(1);
});
