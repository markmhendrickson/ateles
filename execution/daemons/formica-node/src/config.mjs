import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import yaml from "yaml";
import { expandEnvInString } from "./operator_transport.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * @param {unknown} node
 */
function substEnvDeep(node) {
  if (typeof node === "string") return expandEnvInString(node);
  if (Array.isArray(node)) return node.map(substEnvDeep);
  if (node && typeof node === "object") {
    /** @type {Record<string, unknown>} */
    const out = {};
    for (const [k, v] of Object.entries(node)) {
      out[k] = substEnvDeep(v);
    }
    return out;
  }
  return node;
}

/**
 * @returns {Record<string, unknown>}
 */
export function loadConfig() {
  const configPath =
    process.env.FORMICA_CONFIG?.trim() ||
    process.env.ISSUE_PROCESSOR_CONFIG?.trim() ||
    path.join(__dirname, "..", "config.yaml");
  const raw = fs.readFileSync(configPath, "utf8");
  const doc = yaml.parse(raw);
  if (!doc || typeof doc !== "object") {
    throw new Error(`Invalid config: ${configPath}`);
  }
  const cfg = /** @type {Record<string, unknown>} */ (substEnvDeep(doc));
  cfg.neotoma = cfg.neotoma && typeof cfg.neotoma === "object" ? cfg.neotoma : {};
  cfg.neotoma.base_url =
    process.env.NEOTOMA_BASE_URL?.trim() ||
    String(cfg.neotoma.base_url || "http://localhost:3080").replace(/\/$/, "");
  cfg.subscription =
    cfg.subscription && typeof cfg.subscription === "object" ? cfg.subscription : {};
  cfg.processing =
    cfg.processing && typeof cfg.processing === "object" ? cfg.processing : {};
  cfg.repos = cfg.repos && typeof cfg.repos === "object" ? cfg.repos : {};
  cfg.operator_transport =
    cfg.operator_transport && typeof cfg.operator_transport === "object"
      ? cfg.operator_transport
      : {};
  return cfg;
}
