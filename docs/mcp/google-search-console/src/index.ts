import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
    CallToolRequestSchema,
    ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { google } from 'googleapis';
import { z } from "zod";
import { zodToJsonSchema } from "zod-to-json-schema";
import { OAuth2Client } from 'google-auth-library';
import fs from 'fs';
import { watchFile, unwatchFile } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import http from 'http';
import open from 'open';
import os from 'os';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Configuration paths
const CONFIG_DIR = path.join(os.homedir(), '.search-console-mcp');
const OAUTH_PATH = process.env.SEARCH_CONSOLE_OAUTH_PATH || path.join(CONFIG_DIR, 'gcp-oauth.keys.json');
const CREDENTIALS_PATH = process.env.SEARCH_CONSOLE_CREDENTIALS_PATH || path.join(CONFIG_DIR, 'credentials.json');

// Will be set during loadCredentials to the actual path found
let actualCredentialsPath = CREDENTIALS_PATH;

// OAuth2 configuration
let oauth2Client: OAuth2Client;
let searchConsole: ReturnType<typeof google.searchconsole>;

/**
 * Watch credentials file for changes and reload automatically
 */
function setupCredentialsFileWatcher(): void {
    try {
        let lastModified = 0;
        watchFile(actualCredentialsPath, { interval: 2000 }, (curr, prev) => {
            if (curr.mtimeMs > prev.mtimeMs && curr.mtimeMs > lastModified) {
                lastModified = curr.mtimeMs;
                
                setTimeout(() => {
                    try {
                        if (fs.existsSync(actualCredentialsPath)) {
                            const credentials = JSON.parse(fs.readFileSync(actualCredentialsPath, 'utf8'));
                            oauth2Client.setCredentials(credentials);
                            searchConsole = google.searchconsole({ version: 'v1', auth: oauth2Client });
                            process.stderr.write('Search Console credentials reloaded from file.\n');
                        }
                    } catch (error) {
                        process.stderr.write(`Error reloading Search Console credentials: ${error instanceof Error ? error.message : String(error)}\n`);
                    }
                }, 500);
            }
        });
        
        process.stderr.write(`Watching Search Console credentials file for changes: ${actualCredentialsPath}\n`);
    } catch (error) {
        if (error instanceof Error && 'code' in error && error.code !== 'ENOENT') {
            process.stderr.write(`Warning: Could not set up Search Console credentials file watcher: ${error.message}\n`);
        }
    }
}

async function loadCredentials() {
    try {
        if (!process.env.SEARCH_CONSOLE_OAUTH_PATH && !CREDENTIALS_PATH && !fs.existsSync(CONFIG_DIR)) {
            fs.mkdirSync(CONFIG_DIR, { recursive: true });
        }

        // Check multiple locations for OAuth keys
        const possibleOAuthPaths = [
            OAUTH_PATH, // From environment or default config dir
            path.join(process.cwd(), 'gcp-oauth.keys.json'), // Current directory
            path.join(process.cwd(), '.creds', 'gcp-oauth.keys.json'), // Repo .creds directory
            path.join(process.cwd(), '..', '..', '.creds', 'gcp-oauth.keys.json'), // Repo root .creds (if in mcp/google-search-console)
        ];

        let oauthPath = OAUTH_PATH;
        let foundPath: string | null = null;

        for (const possiblePath of possibleOAuthPaths) {
            if (fs.existsSync(possiblePath)) {
                foundPath = possiblePath;
                oauthPath = possiblePath;
                break;
            }
        }

        if (!foundPath) {
            console.error('Error: OAuth keys file not found. Please place gcp-oauth.keys.json in one of:');
            console.error('  - Current directory');
            console.error('  - .creds/gcp-oauth.keys.json (repo root)');
            console.error('  -', CONFIG_DIR);
            console.error('  - Or set SEARCH_CONSOLE_OAUTH_PATH environment variable');
            process.exit(1);
        }

        const keysContent = JSON.parse(fs.readFileSync(oauthPath, 'utf8'));
        const keys = keysContent.installed || keysContent.web;

        if (!keys) {
            console.error('Error: Invalid OAuth keys file format. File should contain either "installed" or "web" credentials.');
            process.exit(1);
        }

        const callback = process.argv[2] === 'auth' && process.argv[3] 
            ? process.argv[3] 
            : "http://localhost:3001/oauth2callback";

        oauth2Client = new OAuth2Client(
            keys.client_id,
            keys.client_secret,
            callback
        );

        // Check for credentials in multiple locations
        const possibleCredPaths = [
            CREDENTIALS_PATH, // From environment or default config dir
            path.join(process.cwd(), '.creds', 'search-console-credentials.json'), // Repo .creds directory
            path.join(process.cwd(), '..', '..', '.creds', 'search-console-credentials.json'), // Repo root .creds
        ];

        let credPath = CREDENTIALS_PATH;
        for (const possiblePath of possibleCredPaths) {
            if (fs.existsSync(possiblePath)) {
                credPath = possiblePath;
                break;
            }
        }

        // Store the actual credentials path for use in authenticate()
        actualCredentialsPath = credPath;

        if (fs.existsSync(credPath)) {
            const credentials = JSON.parse(fs.readFileSync(credPath, 'utf8'));
            oauth2Client.setCredentials(credentials);
        }
        
        searchConsole = google.searchconsole({ version: 'v1', auth: oauth2Client });
        setupCredentialsFileWatcher();
    } catch (error) {
        console.error('Error loading credentials:', error);
        process.exit(1);
    }
}

async function authenticate() {
    const server = http.createServer();
    server.listen(3001);

    return new Promise<void>((resolve, reject) => {
        const authUrl = oauth2Client.generateAuthUrl({
            access_type: 'offline',
            scope: [
                'https://www.googleapis.com/auth/webmasters.readonly',
                'https://www.googleapis.com/auth/webmasters'
            ],
        });

        console.log('Please visit this URL to authenticate:', authUrl);
        open(authUrl);

        server.on('request', async (req, res) => {
            if (!req.url?.startsWith('/oauth2callback')) return;

            const url = new URL(req.url, 'http://localhost:3001');
            const code = url.searchParams.get('code');

            if (!code) {
                res.writeHead(400);
                res.end('No code provided');
                reject(new Error('No code provided'));
                return;
            }

            try {
                const { tokens } = await oauth2Client.getToken(code);
                oauth2Client.setCredentials(tokens);
                searchConsole = google.searchconsole({ version: 'v1', auth: oauth2Client });
                
                // Ensure directory exists before writing
                const credDir = path.dirname(actualCredentialsPath);
                if (!fs.existsSync(credDir)) {
                    fs.mkdirSync(credDir, { recursive: true });
                }
                fs.writeFileSync(actualCredentialsPath, JSON.stringify(tokens));

                res.writeHead(200);
                res.end('Authentication successful! You can close this window.');
                server.close();
                resolve();
            } catch (error) {
                res.writeHead(500);
                res.end('Authentication failed');
                reject(error);
            }
        });
    });
}

// Schema definitions
const ListSitesSchema = z.object({}).describe("Lists all sites in Search Console");

const GetIndexingIssuesSchema = z.object({
    siteUrl: z.string().describe("Site URL (e.g., 'sc-domain:example.com' or 'https://example.com/')"),
    category: z.enum(['page', 'video', 'smartphone']).optional().describe("Issue category filter"),
    severity: z.enum(['error', 'warning']).optional().describe("Issue severity filter"),
}).describe("Gets indexing issues for a site");

const GetSearchAnalyticsSchema = z.object({
    siteUrl: z.string().describe("Site URL (e.g., 'sc-domain:example.com' or 'https://example.com/')"),
    startDate: z.string().describe("Start date in YYYY-MM-DD format"),
    endDate: z.string().describe("End date in YYYY-MM-DD format"),
    dimensions: z.array(z.enum(['query', 'page', 'country', 'device', 'searchAppearance'])).optional().describe("Dimensions to group by"),
    rowLimit: z.number().optional().default(1000).describe("Maximum number of rows to return"),
}).describe("Gets search analytics data");

const InspectUrlSchema = z.object({
    siteUrl: z.string().describe("Site URL (e.g., 'sc-domain:example.com' or 'https://example.com/')"),
    inspectionUrl: z.string().describe("URL to inspect"),
}).describe("Inspects a URL's indexing status");

const ListSitemapsSchema = z.object({
    siteUrl: z.string().describe("Site URL (e.g., 'sc-domain:example.com' or 'https://example.com/')"),
}).describe("Lists all sitemaps for a site");

const SubmitSitemapSchema = z.object({
    siteUrl: z.string().describe("Site URL (e.g., 'sc-domain:example.com' or 'https://example.com/')"),
    feedpath: z.string().describe("Sitemap path (e.g., 'sitemap.xml')"),
}).describe("Submits a sitemap to Search Console");

const RemoveSiteSchema = z.object({
    siteUrl: z.string().describe("Site URL to remove (e.g., 'sc-domain:example.com' or 'https://example.com/')"),
}).describe("Removes a site property from Search Console");

// Main function
async function main() {
    await loadCredentials();

    if (process.argv[2] === 'auth') {
        await authenticate();
        console.log('Authentication completed successfully');
        process.exit(0);
    }

    const server = new Server(
        {
            name: "google-search-console",
            version: "1.0.0",
        },
        {
            capabilities: {
                tools: {},
            },
        }
    );

    // List tools
    server.setRequestHandler(ListToolsRequestSchema, async () => ({
        tools: [
            {
                name: "list_sites",
                description: "Lists all sites/properties in Google Search Console",
                inputSchema: zodToJsonSchema(ListSitesSchema) as any,
            },
            {
                name: "get_indexing_issues",
                description: "Gets indexing issues (404s, redirects, canonical issues, noindex tags) for a site",
                inputSchema: zodToJsonSchema(GetIndexingIssuesSchema) as any,
            },
            {
                name: "get_search_analytics",
                description: "Gets search analytics data (impressions, clicks, CTR, position) for a site",
                inputSchema: zodToJsonSchema(GetSearchAnalyticsSchema) as any,
            },
            {
                name: "inspect_url",
                description: "Inspects a URL's indexing status in Google Search Console",
                inputSchema: zodToJsonSchema(InspectUrlSchema) as any,
            },
            {
                name: "list_sitemaps",
                description: "Lists all sitemaps submitted for a site",
                inputSchema: zodToJsonSchema(ListSitemapsSchema) as any,
            },
            {
                name: "submit_sitemap",
                description: "Submits a sitemap to Google Search Console",
                inputSchema: zodToJsonSchema(SubmitSitemapSchema) as any,
            },
            {
                name: "remove_site",
                description: "Removes a site property from Google Search Console",
                inputSchema: zodToJsonSchema(RemoveSiteSchema) as any,
            },
        ],
    }));

    // Handle tool calls
    server.setRequestHandler(CallToolRequestSchema, async (request) => {
        const { name, arguments: args } = request.params;

        try {
            switch (name) {
                case "list_sites": {
                    const validatedArgs = ListSitesSchema.parse(args);
                    const response = await searchConsole.sites.list();
                    return {
                        content: [
                            {
                                type: "text",
                                text: JSON.stringify({
                                    sites: response.data.siteEntry || [],
                                }, null, 2),
                            },
                        ],
                    };
                }

                case "get_indexing_issues": {
                    const validatedArgs = GetIndexingIssuesSchema.parse(args);
                    
                    // Note: The Search Console API doesn't provide a direct endpoint for bulk indexing issues
                    // Indexing issues are available in the web interface but not via API
                    // We can use URL Inspection API for individual URLs, but not for bulk issue retrieval
                    // This tool provides guidance on how to access issues
                    
                    return {
                        content: [
                            {
                                type: "text",
                                text: JSON.stringify({
                                    siteUrl: validatedArgs.siteUrl,
                                    note: "The Search Console API does not provide bulk indexing issue retrieval. Indexing issues (404s, redirects, canonical issues, noindex tags) are available in the Search Console web interface.",
                                    recommendation: "Use the 'inspect_url' tool to check individual URLs, or access the Search Console web interface for bulk issue reports.",
                                    webInterface: `https://search.google.com/search-console?resource_id=${encodeURIComponent(validatedArgs.siteUrl)}&hl=en`,
                                    category: validatedArgs.category,
                                    severity: validatedArgs.severity,
                                }, null, 2),
                            },
                        ],
                    };
                }

                case "get_search_analytics": {
                    const validatedArgs = GetSearchAnalyticsSchema.parse(args);
                    const response = await searchConsole.searchanalytics.query({
                        siteUrl: validatedArgs.siteUrl,
                        requestBody: {
                            startDate: validatedArgs.startDate,
                            endDate: validatedArgs.endDate,
                            dimensions: validatedArgs.dimensions || [],
                            rowLimit: validatedArgs.rowLimit || 1000,
                        },
                    });
                    
                    return {
                        content: [
                            {
                                type: "text",
                                text: JSON.stringify({
                                    siteUrl: validatedArgs.siteUrl,
                                    data: response.data,
                                }, null, 2),
                            },
                        ],
                    };
                }

                case "inspect_url": {
                    const validatedArgs = InspectUrlSchema.parse(args);
                    const urlInspection = google.searchconsole('v1').urlInspection;
                    
                    const response = await urlInspection.index.inspect({
                        requestBody: {
                            inspectionUrl: validatedArgs.inspectionUrl,
                            siteUrl: validatedArgs.siteUrl,
                            languageCode: 'en-US',
                        },
                    });
                    
                    return {
                        content: [
                            {
                                type: "text",
                                text: JSON.stringify({
                                    siteUrl: validatedArgs.siteUrl,
                                    inspectionUrl: validatedArgs.inspectionUrl,
                                    result: response.data,
                                }, null, 2),
                            },
                        ],
                    };
                }

                case "list_sitemaps": {
                    const validatedArgs = ListSitemapsSchema.parse(args);
                    const response = await searchConsole.sitemaps.list({
                        siteUrl: validatedArgs.siteUrl,
                    });
                    
                    return {
                        content: [
                            {
                                type: "text",
                                text: JSON.stringify({
                                    siteUrl: validatedArgs.siteUrl,
                                    sitemaps: response.data.sitemap || [],
                                }, null, 2),
                            },
                        ],
                    };
                }

                case "submit_sitemap": {
                    const validatedArgs = SubmitSitemapSchema.parse(args);
                    const response = await searchConsole.sitemaps.submit({
                        siteUrl: validatedArgs.siteUrl,
                        feedpath: validatedArgs.feedpath,
                    });
                    
                    return {
                        content: [
                            {
                                type: "text",
                                text: JSON.stringify({
                                    siteUrl: validatedArgs.siteUrl,
                                    feedpath: validatedArgs.feedpath,
                                    success: true,
                                    message: "Sitemap submitted successfully",
                                }, null, 2),
                            },
                        ],
                    };
                }

                case "remove_site": {
                    const validatedArgs = RemoveSiteSchema.parse(args);
                    await searchConsole.sites.delete({
                        siteUrl: validatedArgs.siteUrl,
                    });
                    
                    return {
                        content: [
                            {
                                type: "text",
                                text: JSON.stringify({
                                    siteUrl: validatedArgs.siteUrl,
                                    success: true,
                                    message: "Site removed successfully from Search Console",
                                }, null, 2),
                            },
                        ],
                    };
                }

                default:
                    throw new Error(`Unknown tool: ${name}`);
            }
        } catch (error: any) {
            return {
                content: [
                    {
                        type: "text",
                        text: JSON.stringify({
                            error: error.message,
                            stack: error.stack,
                        }, null, 2),
                    },
                ],
                isError: true,
            };
        }
    });

    // Run server
    const transport = new StdioServerTransport();
    await server.connect(transport);
    console.error("Google Search Console MCP server running on stdio");
}

main().catch((error) => {
    console.error("Fatal error:", error);
    process.exit(1);
});
