/**
 * LOCAL-ONLY MODE for the dashboard server — the persistent console on the
 * operator's own machine (see deploy/ and the README).
 *
 * In this mode the Google sign-in gate is replaced by two things, and only
 * these two are what stands between the operator's task graph and anything
 * else that can reach the machine:
 *
 *   1. The server binds 127.0.0.1 and nothing else. `resolveServeMode` refuses
 *      to produce a config that binds any other address, and serve.ts checks
 *      the bound address again after `listen` and exits if it is not loopback.
 *   2. Every request's Host header must be EXACTLY `127.0.0.1:<port>` or
 *      `localhost:<port>`. Anything else — absent, another name, another port,
 *      a trailing dot — is answered 403 with no body. This is the DNS-rebinding
 *      defence: a hostile page that rebinds its own name to 127.0.0.1 still
 *      sends its own name as Host, and is refused before any route runs.
 *
 * The mode is chosen ONCE, from the process environment at startup. Nothing a
 * request carries (header, query, cookie, path) can select it: the decision is
 * made before the server exists.
 *
 * What it does NOT protect against: any other process running as any user on
 * this machine can connect to 127.0.0.1 and send a correct Host header. On a
 * single-user Mac that is the operator; on a shared machine it is not.
 */

/** The fixed port the README documents for the console. */
export const LOCAL_DEFAULT_PORT = 5273;
/** The deployed (Fly) default, unchanged. */
export const DEPLOYED_DEFAULT_PORT = 8080;
/** The only address local-only mode will bind. */
export const LOOPBACK_BIND = "127.0.0.1";

export type ServeMode =
  | { localOnly: false; host: "0.0.0.0"; port: number }
  | { localOnly: true; host: typeof LOOPBACK_BIND; port: number };

type Env = Record<string, string | undefined>;

/**
 * Decide how the server listens. Throws — so the process exits before binding
 * anything — on any configuration that is ambiguous or would expose local-only
 * mode beyond loopback.
 */
export function resolveServeMode(env: Env): ServeMode {
  const flag = env.DASHBOARD_LOCAL_ONLY;
  if (flag === undefined || flag === "") {
    // The deployed, Google-gated mode: exactly what serve.ts always did.
    return { localOnly: false, host: "0.0.0.0", port: Number(env.PORT) || DEPLOYED_DEFAULT_PORT };
  }
  if (flag !== "1") {
    // "true", "yes", "0" — refuse rather than guess which mode was meant.
    throw new Error(`DASHBOARD_LOCAL_ONLY must be unset or exactly "1" (got a different value). Refusing to start.`);
  }

  const bind = env.DASHBOARD_BIND_HOST ?? LOOPBACK_BIND;
  if (bind !== LOOPBACK_BIND) {
    throw new Error(
      `DASHBOARD_LOCAL_ONLY=1 binds ${LOOPBACK_BIND} only; refusing to bind the non-loopback address "${bind}".`,
    );
  }

  const rawPort = env.PORT ?? String(LOCAL_DEFAULT_PORT);
  const port = Number(rawPort);
  // Port 0 would pick a random port, and the Host check needs to know the
  // exact port the browser will name.
  if (!/^\d+$/.test(rawPort) || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`DASHBOARD_LOCAL_ONLY=1 needs a fixed PORT between 1 and 65535. Refusing to start.`);
  }
  return { localOnly: true, host: LOOPBACK_BIND, port };
}

/** Is the address a socket actually bound to loopback? (Checked after listen.) */
export function isLoopbackAddress(address: string): boolean {
  return address === LOOPBACK_BIND;
}

/** The exact Host values local-only mode accepts. */
export function allowedHosts(port: number): string[] {
  return [`${LOOPBACK_BIND}:${port}`, `localhost:${port}`];
}

/** Exact match only: no case folding, no trimming, no default port. */
export function hostAllowed(host: unknown, port: number): boolean {
  return typeof host === "string" && allowedHosts(port).includes(host);
}

/**
 * Refuse a request whose Host is not one of the two exact local values.
 * Returns true when the request was refused (and answered).
 */
export function refuseForeignHost(req: { headers: Record<string, unknown> }, res: any, port: number): boolean {
  if (hostAllowed(req.headers.host, port)) return false;
  res.statusCode = 403;
  res.end();
  return true;
}
