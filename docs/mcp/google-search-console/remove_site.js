#!/usr/bin/env node
/**
 * Script to remove a site from Google Search Console
 * Usage: node remove_site.js <siteUrl>
 */

import { google } from 'googleapis';
import { OAuth2Client } from 'google-auth-library';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import os from 'os';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '../..');

// Load credentials - check multiple locations
const OAUTH_PATH = process.env.SEARCH_CONSOLE_OAUTH_PATH || 
    (fs.existsSync(path.join(REPO_ROOT, '.creds', 'gcp-oauth.keys.json')) 
        ? path.join(REPO_ROOT, '.creds', 'gcp-oauth.keys.json')
        : path.join(os.homedir(), '.search-console-mcp', 'gcp-oauth.keys.json'));

const CREDENTIALS_PATH = process.env.SEARCH_CONSOLE_CREDENTIALS_PATH ||
    (fs.existsSync(path.join(REPO_ROOT, '.creds', 'search-console-credentials.json'))
        ? path.join(REPO_ROOT, '.creds', 'search-console-credentials.json')
        : path.join(os.homedir(), '.search-console-mcp', 'credentials.json'));

async function removeSite(siteUrl) {
    // Load OAuth keys
    if (!fs.existsSync(OAUTH_PATH)) {
        console.error(`Error: OAuth keys not found at ${OAUTH_PATH}`);
        process.exit(1);
    }

    const keysContent = JSON.parse(fs.readFileSync(OAUTH_PATH, 'utf8'));
    const keys = keysContent.installed || keysContent.web;
    const oauth2Client = new OAuth2Client(keys.client_id, keys.client_secret);

    // Load credentials
    if (!fs.existsSync(CREDENTIALS_PATH)) {
        console.error(`Error: Credentials not found at ${CREDENTIALS_PATH}`);
        console.error('Please run: npm run auth');
        process.exit(1);
    }

    const credentials = JSON.parse(fs.readFileSync(CREDENTIALS_PATH, 'utf8'));
    oauth2Client.setCredentials(credentials);

    const searchConsole = google.searchconsole({ version: 'v1', auth: oauth2Client });

    try {
        console.log(`Removing site: ${siteUrl}...`);
        await searchConsole.sites.delete({
            siteUrl: siteUrl,
        });
        console.log(`✓ Successfully removed ${siteUrl} from Search Console`);
    } catch (error) {
        console.error(`✗ Error removing ${siteUrl}:`, error.message);
        process.exit(1);
    }
}

// Get site URL from command line
const siteUrl = process.argv[2];
if (!siteUrl) {
    console.error('Usage: node remove_site.js <siteUrl>');
    console.error('Example: node remove_site.js "sc-domain:liveultimate.com"');
    process.exit(1);
}

removeSite(siteUrl).catch(console.error);
