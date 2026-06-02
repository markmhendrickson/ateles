#!/usr/bin/env node
/**
 * Simple Neotoma HTTP query helper for agents
 */

import http from 'http';
import https from 'https';
import { readFileSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

// Load environment
const envPath = join(homedir(), '.config', 'neotoma', '.env');
try {
  const envContent = readFileSync(envPath, 'utf-8');
  for (const line of envContent.split('\n')) {
    const trimmed = line.trim();
    if (trimmed && !trimmed.startsWith('#') && trimmed.includes('=')) {
      const [key, ...valueParts] = trimmed.split('=');
      const value = valueParts.join('=').replace(/^["']|["']$/g, '');
      if (!process.env[key.trim()]) {
        process.env[key.trim()] = value;
      }
    }
  }
} catch (err) {
  // Silent fail
}

const NEOTOMA_BASE_URL = process.env.NEOTOMA_BASE_URL || 'https://neotoma.markmhendrickson.com';
const NEOTOMA_BEARER_TOKEN = process.env.NEOTOMA_BEARER_TOKEN;

function neotomaRequest(path, method = 'GET', body = null) {
  const url = new URL(path, NEOTOMA_BASE_URL);
  const isHttps = url.protocol === 'https:';
  const httpModule = isHttps ? https : http;

  return new Promise((resolve, reject) => {
    const options = {
      hostname: url.hostname,
      port: url.port || (isHttps ? 443 : 80),
      path: url.pathname + url.search,
      method: method,
      headers: {
        'Authorization': `Bearer ${NEOTOMA_BEARER_TOKEN}`,
        'Content-Type': 'application/json',
      },
    };

    const req = httpModule.request(options, (res) => {
      let data = '';

      res.on('data', (chunk) => {
        data += chunk;
      });

      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try {
            resolve(JSON.parse(data));
          } catch (err) {
            resolve(data);
          }
        } else {
          reject(new Error(`HTTP ${res.statusCode}: ${data}`));
        }
      });
    });

    req.on('error', reject);

    if (body) {
      req.write(JSON.stringify(body));
    }

    req.end();
  });
}

export async function searchByEmail(email) {
  const params = new URLSearchParams({
    search: email,
    entity_type: 'person',
    limit: '5',
    include_snapshots: 'true',
  });

  return neotomaRequest(`/entities?${params}`);
}

export async function searchByName(name, entityType = 'person') {
  const params = new URLSearchParams({
    search: name,
    entity_type: entityType,
    limit: '5',
    include_snapshots: 'true',
  });

  return neotomaRequest(`/entities?${params}`);
}

export async function createEntity(entityType, snapshot, relationships = []) {
  return neotomaRequest('/observations', 'POST', {
    entity_type: entityType,
    ...snapshot,
    relationships,
  });
}

export async function searchCompany(name) {
  return searchByName(name, 'company');
}

// CLI support
if (import.meta.url === `file://${process.argv[1]}`) {
  const command = process.argv[2];
  const arg = process.argv[3];

  try {
    let result;
    if (command === 'search-email') {
      result = await searchByEmail(arg);
    } else if (command === 'search-name') {
      result = await searchByName(arg);
    } else if (command === 'search-company') {
      result = await searchCompany(arg);
    } else {
      console.error('Usage: neotoma_query.mjs {search-email|search-name|search-company} <query>');
      process.exit(1);
    }
    console.log(JSON.stringify(result, null, 2));
  } catch (err) {
    console.error('Error:', err.message);
    process.exit(1);
  }
}
