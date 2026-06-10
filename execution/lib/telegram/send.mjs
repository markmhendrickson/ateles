#!/usr/bin/env node
/**
 * execution/lib/telegram/send.mjs
 *
 * Simple Telegram message sender for Ateles daemons and agents.
 *
 * Usage:
 *   node send.mjs --text "Message text"
 *   node send.mjs --text "Message" --topic TOPIC_ID
 *
 * Environment variables:
 *   TELEGRAM_BOT_TOKEN    Bot token from @BotFather
 *   TELEGRAM_CHAT_ID      Target chat ID
 *   TELEGRAM_TOPIC_*      Topic IDs (optional, for topic-specific routing)
 */

import https from 'https';
import { readFileSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

// Load environment from multiple locations
const envPaths = [
  join(homedir(), '.config', 'neotoma', '.env'),
  join(homedir(), 'repos', 'ateles-private', '.env'),
  join(homedir(), 'repos', 'openclaw', '.env'),
];

for (const envPath of envPaths) {
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
    // Silent fail - try next path
  }
}

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const CHAT_ID = process.env.TELEGRAM_CHAT_ID;

function parseArgs() {
  const args = process.argv.slice(2);
  const parsed = {
    text: '',
    topic: null,
    plain: false,
    html: false,
  };

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--text' && i + 1 < args.length) {
      parsed.text = args[i + 1];
      i++;
    } else if (args[i] === '--topic' && i + 1 < args.length) {
      parsed.topic = args[i + 1];
      i++;
    } else if (args[i] === '--plain') {
      // Send as plain text — no Telegram Markdown parsing. Use this when the
      // message body may contain literal '*' or '_' (e.g. **bold**, REFERS_TO)
      // that legacy Markdown would mangle into stray formatting.
      parsed.plain = true;
    } else if (args[i] === '--html') {
      // Send with parse_mode=HTML. Caller is responsible for escaping <>&
      // in literal text and emitting only Telegram-supported HTML tags
      // (<b> <i> <u> <s> <a> <code> <pre> <blockquote>). Unlike Markdown,
      // HTML mode leaves '*' and '_' untouched, so it is safe for bodies
      // containing REFERS_TO / **emphasis** as long as <>& are escaped.
      parsed.html = true;
    }
  }

  return parsed;
}

function sendTelegramMessage(text, messageThreadId = null, plain = false, html = false) {
  if (!BOT_TOKEN || !CHAT_ID) {
    console.error('Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set');
    process.exit(1);
  }

  const payload = {
    chat_id: CHAT_ID,
    text: text,
  };

  if (html) {
    payload.parse_mode = 'HTML';
  } else if (!plain) {
    payload.parse_mode = 'Markdown';
  }

  if (messageThreadId) {
    payload.message_thread_id = messageThreadId;
  }

  const data = JSON.stringify(payload);

  const options = {
    hostname: 'api.telegram.org',
    port: 443,
    path: `/bot${BOT_TOKEN}/sendMessage`,
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(data),
    },
  };

  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let body = '';

      res.on('data', (chunk) => {
        body += chunk;
      });

      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(JSON.parse(body));
        } else {
          reject(new Error(`Telegram API error: ${res.statusCode} ${body}`));
        }
      });
    });

    req.on('error', (err) => {
      reject(err);
    });

    req.write(data);
    req.end();
  });
}

async function main() {
  const { text, topic, plain, html } = parseArgs();

  if (!text) {
    console.error('Usage: node send.mjs --text "Message text" [--topic TOPIC_ID] [--plain] [--html]');
    process.exit(1);
  }

  try {
    const result = await sendTelegramMessage(text, topic, plain, html);
    console.log('Message sent successfully:', result.result.message_id);
  } catch (err) {
    console.error('Failed to send message:', err.message);
    process.exit(1);
  }
}

main();
