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
 *   out: { "status": 200, "ok": true, "body": "<response text>" }
 *        { "error": "<message>" }   (exit 1)
 *
 * Note: what a signed write may touch is decided server-side by the agent_grant
 * the signature is admitted against (its ops and entity types). The server's
 * NEOTOMA_STRICT_AAUTH_SUBS does not promote a tier; it refuses an unsigned
 * request whose X-Agent-Label names a listed sub. Tier promotion comes from
 * NEOTOMA_OPERATOR_ATTESTED_SUBS. New daemon writes use the in-process Python
 * signer (neotoma_signed.NeotomaWriter), not this helper.
 */

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

async function main() {
  const spec = JSON.parse(await readStdin());
  if (!spec.url) throw new Error("request spec missing 'url'");
  const { cliSignedFetch } = await loadSigner();
  const resp = await cliSignedFetch(spec.url, {
    method: (spec.method || "GET").toUpperCase(),
    headers: spec.headers || {},
    body: spec.body,
  });
  const text = await resp.text();
  process.stdout.write(JSON.stringify({ status: resp.status, ok: resp.ok, body: text }));
}

main().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String(err && err.message ? err.message : err) }));
  process.exit(1);
});
