#!/usr/bin/env node
/**
 * Script to pull data from Google Search Console
 * Usage: node pull_data.js [siteUrl] [startDate] [endDate]
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

async function main() {
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

    // Get sites
    console.log('Fetching sites...');
    const sitesResponse = await searchConsole.sites.list();
    const sites = sitesResponse.data.siteEntry || [];
    
    console.log(`\nFound ${sites.length} site(s):\n`);
    sites.forEach((site, i) => {
        console.log(`${i + 1}. ${site.siteUrl} (${site.permissionLevel})`);
    });

    if (sites.length === 0) {
        console.log('No sites found.');
        return;
    }

    // Get search analytics for each site (last 30 days)
    const endDate = new Date();
    const startDate = new Date();
    startDate.setDate(startDate.getDate() - 30);

    const startDateStr = startDate.toISOString().split('T')[0];
    const endDateStr = endDate.toISOString().split('T')[0];

    console.log(`\nFetching search analytics (${startDateStr} to ${endDateStr})...\n`);

    for (const site of sites) {
        try {
            console.log(`\n=== ${site.siteUrl} ===`);
            const analyticsResponse = await searchConsole.searchanalytics.query({
                siteUrl: site.siteUrl,
                requestBody: {
                    startDate: startDateStr,
                    endDate: endDateStr,
                    dimensions: ['query', 'page'],
                    rowLimit: 10,
                },
            });

            const rows = analyticsResponse.data.rows || [];
            if (rows.length === 0) {
                console.log('  No data available');
                continue;
            }

            console.log(`\n  Top queries:`);
            rows.slice(0, 5).forEach((row, i) => {
                const keys = row.keys || [];
                const query = keys.find(k => k.startsWith('http') ? false : true) || keys[0] || 'N/A';
                console.log(`    ${i + 1}. "${query}" - ${row.clicks || 0} clicks, ${row.impressions || 0} impressions, CTR: ${((row.ctr || 0) * 100).toFixed(2)}%`);
            });

            // Get summary
            const summary = {
                clicks: rows.reduce((sum, r) => sum + (r.clicks || 0), 0),
                impressions: rows.reduce((sum, r) => sum + (r.impressions || 0), 0),
                ctr: rows.length > 0 ? rows.reduce((sum, r) => sum + (r.ctr || 0), 0) / rows.length : 0,
                position: rows.length > 0 ? rows.reduce((sum, r) => sum + (r.position || 0), 0) / rows.length : 0,
            };

            console.log(`\n  Summary:`);
            console.log(`    Total clicks: ${summary.clicks}`);
            console.log(`    Total impressions: ${summary.impressions}`);
            console.log(`    Average CTR: ${(summary.ctr * 100).toFixed(2)}%`);
            console.log(`    Average position: ${summary.position.toFixed(1)}`);

        } catch (error) {
            console.error(`  Error fetching data for ${site.siteUrl}:`, error.message);
        }
    }
}

main().catch(console.error);
