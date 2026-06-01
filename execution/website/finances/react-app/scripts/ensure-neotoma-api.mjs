#!/usr/bin/env node
/**
 * Before `vite` / `vite preview`, ensure the Neotoma API this app proxies to is up
 * and uses **local production** configuration from the monorepo root (`.env` → `NEOTOMA_*`, etc.).
 *
 * Default: **FINANCES_NEOTOMA_LOCAL_PROD_BOOT** is on (unset = true): `neotoma prod api stop` then
 * `neotoma prod api start --background` with `cwd` = repo root so the CLI picks up the same local prod
 * data dir as the rest of ateles. Opt out: `FINANCES_NEOTOMA_LOCAL_PROD_BOOT=0` in `.env.local` or env.
 *
 * Reads `VITE_NEOTOMA_API_URL` from env or `.env` / `.env.local` in this app (same as Vite).
 * For localhost **3080**, uses `neotoma dev` stop/start instead of prod.
 *
 * Skip entirely: `SKIP_NEOTOMA_ENSURE=1`
 * Remote API URL: only checks `/health`; does not stop/start.
 */

import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const APP_ROOT = resolve(__dirname, '..')

const PREFIX = '[finances]'
const NEOTOMA_CLI_TIMEOUT_MS = 30000
const NEOTOMA_START_TIMEOUT_MS = 4000

function loadDotEnvFiles(dir) {
  for (const name of ['.env.local', '.env']) {
    const p = resolve(dir, name)
    if (!existsSync(p)) continue
    for (const line of readFileSync(p, 'utf8').split('\n')) {
      const trimmed = line.trim()
      if (!trimmed || trimmed.startsWith('#')) continue
      const eq = trimmed.indexOf('=')
      if (eq <= 0) continue
      const key = trimmed.slice(0, eq).trim()
      let val = trimmed.slice(eq + 1).trim()
      if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
        val = val.slice(1, -1)
      }
      if (process.env[key] === undefined) process.env[key] = val
    }
  }
}

/** Walk up from `start` until `.git` exists (monorepo root). */
function findRepoRoot(startDir) {
  let d = resolve(startDir)
  for (let i = 0; i < 14; i++) {
    if (existsSync(join(d, '.git'))) return d
    const parent = dirname(d)
    if (parent === d) break
    d = parent
  }
  return null
}

/** Keys typically needed for Neotoma prod API to use the intended local DB (see ateles `.env`). */
function loadNeotomaEnvFromRepo(repoRoot) {
  if (!repoRoot) return {}
  const p = join(repoRoot, '.env')
  if (!existsSync(p)) return {}
  const out = {}
  for (const line of readFileSync(p, 'utf8').split('\n')) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const eq = trimmed.indexOf('=')
    if (eq <= 0) continue
    const key = trimmed.slice(0, eq).trim()
    if (!key.startsWith('NEOTOMA_')) continue
    let val = trimmed.slice(eq + 1).trim()
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1)
    }
    out[key] = val
  }
  return out
}

function apiBaseUrl() {
  const raw = (process.env.VITE_NEOTOMA_API_URL || 'http://localhost:3180').trim()
  try {
    return new URL(raw)
  } catch {
    console.error(`${PREFIX} Invalid VITE_NEOTOMA_API_URL: ${raw}`)
    process.exit(1)
  }
}

function isLocalHost(hostname) {
  const h = (hostname || '').toLowerCase()
  return h === 'localhost' || h === '127.0.0.1' || h === '::1'
}

function effectivePort(url) {
  if (url.port) return url.port
  return url.protocol === 'https:' ? '443' : '80'
}

function shouldBootLocalProd() {
  const v = process.env.FINANCES_NEOTOMA_LOCAL_PROD_BOOT
  if (v === '0' || v === 'false') return false
  if (v === '1' || v === 'true') return true
  return true
}

async function healthOk(baseHref) {
  const base = baseHref.replace(/\/$/, '')
  const ac = new AbortController()
  const t = setTimeout(() => ac.abort(), 2500)
  try {
    const res = await fetch(`${base}/health`, { signal: ac.signal })
    clearTimeout(t)
    if (!res.ok) return false
    const body = await res.json().catch(() => ({}))
    return body && body.ok === true
  } catch {
    clearTimeout(t)
    return false
  }
}

async function waitForHealth(baseHref, attempts = 60, delayMs = 500) {
  for (let i = 0; i < attempts; i++) {
    if (await healthOk(baseHref)) return true
    await new Promise((r) => setTimeout(r, delayMs))
  }
  return false
}

function neotomaSpawn(args, cwd, extraEnv, options = {}) {
  const env = { ...process.env, ...extraEnv }
  const cmd = `neotoma ${args.join(' ')}`
  const timeoutMs = Number(options.timeoutMs ?? NEOTOMA_CLI_TIMEOUT_MS)
  console.log(`${PREFIX} Running: ${cmd}`)
  const res = spawnSync('neotoma', args, {
    stdio: 'inherit',
    shell: false,
    cwd,
    env,
    timeout: timeoutMs,
    killSignal: 'SIGKILL',
  })
  const timedOut = Boolean(res.error && res.error.code === 'ETIMEDOUT')
  if (res.error) {
    if (timedOut) {
      console.warn(`${PREFIX} Command timed out after ${timeoutMs}ms: ${cmd}`)
    } else {
      console.error(`${PREFIX} Command failed: ${cmd}`)
      console.error(String(res.error.message || res.error))
    }
  }
  if (typeof res.status === 'number') {
    console.log(`${PREFIX} Exit ${res.status}: ${cmd}`)
  }
  return {
    res,
    timedOut,
    ok: timedOut || (!res.error && res.status === 0),
  }
}

function killPortListeners(port) {
  const p = String(port)
  const list = spawnSync('lsof', ['-nP', '-ti', `tcp:${p}`], {
    stdio: ['ignore', 'pipe', 'pipe'],
    shell: false,
  })
  if (list.error) {
    console.warn(`${PREFIX} Could not inspect port ${p} listeners via lsof: ${String(list.error.message || list.error)}`)
    return
  }
  const pids = String(list.stdout || '')
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
    .filter((v, i, arr) => arr.indexOf(v) === i)

  if (pids.length === 0) {
    console.log(`${PREFIX} No existing listeners on port ${p}`)
    return
  }

  console.warn(`${PREFIX} Found ${pids.length} listener(s) on :${p}; terminating: ${pids.join(', ')}`)
  for (const pid of pids) {
    try {
      process.kill(Number(pid), 'SIGTERM')
    } catch {
      // ignore missing/permission issues; subsequent health checks will catch failures
    }
  }
}

function stopStartLocalNeotoma(port, repoRoot, neotomaRepoEnv, boot) {
  const p = String(port)
  const prod = p === '3180'
  const dev = p === '3080'
  if (!prod && !dev) {
    console.error(
      `${PREFIX} Unsupported API port ${p} for local Neotoma. Use http://localhost:3180 (prod) or http://localhost:3080 (dev), or set FINANCES_NEOTOMA_LOCAL_PROD_BOOT=0 and start Neotoma manually.`,
    )
    return false
  }

  const cwd = repoRoot || APP_ROOT
  const envExtra = neotomaRepoEnv
  const startArgs = prod
    ? ['prod', '--no-update-check', 'api', 'start', '--background']
    : ['dev', '--no-update-check', 'api', 'start', '--background']
  const label = prod ? 'prod (local)' : 'dev'

  if (boot) {
    console.warn(`${PREFIX} Local ${label} Neotoma: clearing port ${p}, then starting with cwd ${cwd}`)
    killPortListeners(p)
    const st = neotomaSpawn(startArgs, cwd, envExtra, { timeoutMs: NEOTOMA_START_TIMEOUT_MS })
    // Some Neotoma CLI builds leave file handles open after printing "API server started";
    // treat timeout here as non-fatal and rely on /health checks below.
    return st.ok
  }

  const startRes = neotomaSpawn(startArgs, cwd, envExtra, { timeoutMs: NEOTOMA_START_TIMEOUT_MS })
  return startRes.ok
}

async function main() {
  if (process.env.SKIP_NEOTOMA_ENSURE === '1' || process.env.SKIP_NEOTOMA_ENSURE === 'true') {
    console.warn(`${PREFIX} SKIP_NEOTOMA_ENSURE set; skipping Neotoma check.`)
    return
  }

  loadDotEnvFiles(APP_ROOT)
  const boot = shouldBootLocalProd()
  const repoRoot = findRepoRoot(APP_ROOT)
  const neotomaRepoEnv = loadNeotomaEnvFromRepo(repoRoot)
  if (repoRoot && Object.keys(neotomaRepoEnv).length > 0) {
    console.log(`${PREFIX} Loaded ${Object.keys(neotomaRepoEnv).length} NEOTOMA_* var(s) from monorepo .env (passed to Neotoma CLI when stop/start runs)`)
  } else if (!repoRoot) {
    console.warn(`${PREFIX} Could not find monorepo root (.git); Neotoma start uses cwd ${APP_ROOT} only`)
  }

  const url = apiBaseUrl()
  const base = url.toString().replace(/\/$/, '')
  const port = effectivePort(url)

  if (!isLocalHost(url.hostname)) {
    if (await healthOk(base)) {
      console.log(`${PREFIX} Neotoma API OK at ${base}`)
      return
    }
    console.error(
      `${PREFIX} Neotoma API not reachable at ${base} (remote host). Fix URL, network, or VPN; local stop/start only runs for localhost.`,
    )
    process.exit(1)
  }

  const cwd = repoRoot || APP_ROOT

  if (boot) {
    if (!stopStartLocalNeotoma(port, repoRoot, neotomaRepoEnv, true)) {
      process.exit(1)
    }
    if (await waitForHealth(base)) {
      console.log(`${PREFIX} Neotoma API OK at ${base}`)
      return
    }
    console.error(
      `${PREFIX} Timed out waiting for Neotoma at ${base}. Try: neotoma prod api logs --follow (or neotoma dev api logs --follow)`,
    )
    process.exit(1)
  }

  if (await healthOk(base)) {
    console.log(`${PREFIX} Neotoma API OK at ${base}`)
    return
  }

  console.warn(`${PREFIX} API not reachable; starting Neotoma (${port === '3080' ? 'dev' : 'prod'}) in background...`)
  if (!stopStartLocalNeotoma(port, repoRoot, neotomaRepoEnv, false)) {
    process.exit(1)
  }

  if (await waitForHealth(base)) {
    console.log(`${PREFIX} Neotoma API OK at ${base}`)
    return
  }

  console.error(`${PREFIX} Timed out waiting for Neotoma at ${base}.`)
  process.exit(1)
}

await main()
