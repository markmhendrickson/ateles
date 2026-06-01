#!/usr/bin/env python3
"""
Generic Authenticated HTTP Proxy for MCP Servers

This script wraps any stdio MCP server with authentication options:
- MCP standard Bearer token authentication
- OAuth 2.0 authorization code flow (for web usage)

Uses MCP standard: Authorization: Bearer <token> (OAuth 2.1 style)

Usage (Bearer Token):
    python mcp_authenticated_proxy.py \
        --server-name "parquet" \
        --server-script "/path/to/server.py" \
        --port 8080 \
        --auth-token "your-secret-token"

Usage (OAuth 2.0):
    python mcp_authenticated_proxy.py \
        --server-name "parquet" \
        --server-script "/path/to/server.py" \
        --port 8080 \
        --oauth-client-id "your-client-id" \
        --oauth-client-secret "your-client-secret" \
        --oauth-redirect-uri "https://your-tunnel-url.trycloudflare.com/oauth/callback"
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# #region agent log
DEBUG_LOG_PATH = Path("/Users/markmhendrickson/repos/ateles/.cursor/debug.log")


def debug_log(location, message, data, hypothesis_id=None):
    try:
        log_entry = {
            "timestamp": int(datetime.now().timestamp() * 1000),
            "location": location,
            "message": message,
            "data": data,
            "sessionId": "debug-session",
            "runId": "run1",
        }
        if hypothesis_id:
            log_entry["hypothesisId"] = hypothesis_id
        with open(DEBUG_LOG_PATH, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass


# #endregion

try:
    from aiohttp import ClientError, ClientSession, web
    from aiohttp.web_request import Request

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False


# Default configuration
DEFAULT_PORT = 8080
DEFAULT_HOST = "0.0.0.0"
# MCP standard uses Authorization: Bearer <token> (OAuth 2.1 style)
AUTH_HEADER = "Authorization"

# OAuth 2.0 state storage (in-memory, for demo - use Redis/DB in production)
oauth_states: dict[str, str] = {}  # state -> redirect_uri
oauth_tokens: dict[str, dict] = {}  # access_token -> token_info
oauth_codes: dict[
    str, dict
] = {}  # code -> {state, redirect_uri, code_challenge, code_challenge_method}

# Configure logging
LOG_FILE = os.environ.get("MCP_PROXY_LOG_FILE", "/private/tmp/mcp_proxy.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


def generate_auth_token() -> str:
    """Generate a secure random auth token."""
    return secrets.token_urlsafe(32)


def get_auth_token_from_env(server_name: str) -> Optional[str]:
    """Get MCP auth token from environment variable or 1Password.

    Uses MCP_AUTH_TOKEN (standard) or server-specific token.
    """
    # Try server-specific token first (e.g., MCP_PARQUET_AUTH_TOKEN)
    server_token = os.environ.get(f"MCP_{server_name.upper()}_AUTH_TOKEN")
    if server_token:
        return server_token

    # Try MCP_AUTH_TOKEN (MCP standard, shared across servers)
    token = os.environ.get("MCP_AUTH_TOKEN")
    if token:
        return token

    # Backward compatibility: try MCP_PROXY_API_KEY
    token = os.environ.get("MCP_PROXY_API_KEY")
    if token:
        return token

    # Try 1Password (if op CLI is available)
    try:
        if os.path.exists("/usr/local/bin/op") or os.path.exists(
            "/opt/homebrew/bin/op"
        ):
            # Try server-specific 1Password item first
            result = subprocess.run(
                [
                    "op",
                    "read",
                    f"op://Private/{server_name.title()} MCP Proxy/MCP_AUTH_TOKEN",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()

            # Try generic MCP auth token
            result = subprocess.run(
                ["op", "read", "op://Private/MCP Proxy/MCP_AUTH_TOKEN"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()

            # Fallback to old field name
            result = subprocess.run(
                ["op", "read", f"op://Private/{server_name.title()} MCP Proxy/API Key"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
    except Exception:
        pass

    return None


async def handle_request(
    request: Request,
    auth_token: Optional[str],
    proxy_url: str,
    oauth_client_id: Optional[str] = None,
    oauth_client_secret: Optional[str] = None,
    oauth_redirect_uri: Optional[str] = None,
) -> web.Response:
    """Handle HTTP request with Bearer token or OAuth authentication."""
    # Log incoming request
    logger.info(f"Request: {request.method} {request.path_qs} from {request.remote}")
    logger.debug(f"Headers: {dict(request.headers)}")

    # Check Authorization header (MCP standard: Authorization: Bearer <token>)
    auth_header = request.headers.get(AUTH_HEADER, "")

    # Extract Bearer token
    if auth_header.startswith("Bearer "):
        provided_token = auth_header[7:]  # Remove "Bearer " prefix
    else:
        provided_token = None

    # #region agent log
    debug_log(
        "mcp_authenticated_proxy.py:160",
        "Request authentication check",
        {
            "method": request.method,
            "path": request.path,
            "has_auth_header": bool(auth_header),
            "has_provided_token": bool(provided_token),
            "token_prefix": provided_token[:10] if provided_token else None,
            "token_length": len(provided_token) if provided_token else 0,
            "user_agent": request.headers.get("User-Agent", "")[:50],
        },
        "B",
    )
    # #endregion

    # Validate token (Bearer token or OAuth access token)
    if provided_token:
        # Check if it's a valid Bearer token
        if auth_token and provided_token == auth_token:
            # Valid Bearer token
            logger.info(f"Valid Bearer token authentication for {request.path}")
            # #region agent log
            debug_log(
                "mcp_authenticated_proxy.py:172",
                "Bearer token validated",
                {"path": request.path},
                "E",
            )
            # #endregion
        # Check if it's a valid OAuth access token
        elif oauth_client_id and provided_token in oauth_tokens:
            # Valid OAuth token
            logger.info(f"Valid OAuth token authentication for {request.path}")
            # #region agent log
            token_info = oauth_tokens.get(provided_token, {})
            debug_log(
                "mcp_authenticated_proxy.py:177",
                "OAuth token validated",
                {
                    "path": request.path,
                    "token_in_store": True,
                    "has_refresh_token": "refresh_token" in token_info,
                    "scope": token_info.get("scope"),
                },
                "E",
            )
            # #endregion
        else:
            logger.warning(
                f"Invalid token provided for {request.path} (token prefix: {provided_token[:10]}...)"
            )
            # #region agent log
            debug_log(
                "mcp_authenticated_proxy.py:185",
                "Invalid token provided",
                {
                    "path": request.path,
                    "token_in_oauth_store": (
                        provided_token in oauth_tokens if oauth_client_id else False
                    ),
                    "token_matches_bearer": (
                        provided_token == auth_token if auth_token else False
                    ),
                    "oauth_tokens_count": len(oauth_tokens) if oauth_client_id else 0,
                },
                "E",
            )
            # #endregion

            # Return 401 with proper WWW-Authenticate header per MCP spec
            www_authenticate = "Bearer"
            if oauth_client_id and request.path == "/mcp":
                # Use X-Forwarded-Proto to ensure HTTPS URL (Cloudflare sets this)
                proto = request.headers.get("X-Forwarded-Proto", request.scheme)
                if (
                    proto == "https"
                    or request.headers.get("Cf-Visitor", "").find("https") != -1
                ):
                    base_url = f"https://{request.host}"
                else:
                    base_url = f"{proto}://{request.host}"
                resource_metadata_url = (
                    f"{base_url}/.well-known/oauth-protected-resource/mcp"
                )
                www_authenticate = f'Bearer resource_metadata="{resource_metadata_url}"'

            return web.Response(
                text='{"error": "Unauthorized. Missing or invalid authorization token."}',
                status=401,
                content_type="application/json",
                headers={"WWW-Authenticate": www_authenticate},
            )
    else:
        logger.warning(f"No authorization token provided for {request.path}")
        # #region agent log
        debug_log(
            "mcp_authenticated_proxy.py:199",
            "No token provided",
            {
                "method": request.method,
                "path": request.path,
                "all_headers": {
                    k: v[:50] if isinstance(v, str) else str(v)[:50]
                    for k, v in dict(request.headers).items()
                },
            },
            "C",
        )
        # #endregion

        # Return 401 with proper WWW-Authenticate header per MCP spec
        # Include resource_metadata pointing to discovery endpoint to help clients retry after OAuth
        www_authenticate = "Bearer"
        if oauth_client_id and request.path == "/mcp":
            # For MCP endpoints, include resource_metadata in WWW-Authenticate per MCP spec
            # This helps clients discover OAuth endpoints and retry after authentication
            # Use X-Forwarded-Proto to ensure HTTPS URL (Cloudflare sets this)
            proto = request.headers.get("X-Forwarded-Proto", request.scheme)
            if (
                proto == "https"
                or request.headers.get("Cf-Visitor", "").find("https") != -1
            ):
                base_url = f"https://{request.host}"
            else:
                base_url = f"{proto}://{request.host}"
            resource_metadata_url = (
                f"{base_url}/.well-known/oauth-protected-resource/mcp"
            )
            www_authenticate = f'Bearer resource_metadata="{resource_metadata_url}"'

        return web.Response(
            text='{"error": "Unauthorized. Missing or invalid authorization token."}',
            status=401,
            content_type="application/json",
            headers={"WWW-Authenticate": www_authenticate},
        )

    # Forward request to underlying MCP proxy
    # Note: Both GET and POST /mcp are forwarded (official MCP example pattern)
    # GET /mcp is used for session management/SSE streaming, POST /mcp for JSON-RPC requests
    # #region agent log
    if request.path == "/mcp":
        debug_log(
            "mcp_authenticated_proxy.py:280",
            "Request to /mcp endpoint",
            {
                "method": request.method,
                "has_token": bool(provided_token),
                "token_valid": (
                    provided_token in oauth_tokens
                    if (provided_token and oauth_client_id)
                    else False
                ),
                "token_prefix": provided_token[:10] if provided_token else None,
                "oauth_tokens_count": len(oauth_tokens) if oauth_client_id else 0,
            },
            "B" if request.method == "GET" else "D",
        )
    # #endregion
    try:
        async with ClientSession() as session:
            # Forward the request
            method = request.method
            url = f"{proxy_url}{request.path_qs}"
            headers = dict(request.headers)
            # Keep Authorization header for MCP protocol compliance

            # Get request body if present
            body = await request.read() if request.can_read_body else None
            logger.debug(f"Forwarding {method} request to {url}")

            # #region agent log
            if request.path == "/mcp":
                body_str = body.decode("utf-8") if body else None
                is_initialize = False
                if body_str:
                    try:
                        body_json = json.loads(body_str)
                        is_initialize = body_json.get("method") == "initialize"
                    except:
                        pass
                debug_log(
                    "mcp_authenticated_proxy.py:300",
                    "Forwarding request to MCP server",
                    {
                        "method": method,
                        "path": request.path,
                        "has_body": bool(body),
                        "body_preview": body_str[:200] if body_str else None,
                        "is_initialize": is_initialize,
                        "has_auth_header": "Authorization" in headers,
                        "auth_header_present": bool(headers.get("Authorization")),
                    },
                    "B" if method == "GET" else "C",
                )
            # #endregion

            async with session.request(
                method=method,
                url=url,
                headers=headers,
                data=body,
            ) as response:
                response_body = await response.read()
                logger.info(f"Response: {response.status} for {request.path}")

                # #region agent log
                if request.path == "/mcp":
                    response_preview = None
                    try:
                        response_text = (
                            response_body.decode("utf-8")[:500]
                            if response_body
                            else None
                        )
                        response_preview = response_text
                    except:
                        response_preview = (
                            f"<binary {len(response_body)} bytes>"
                            if response_body
                            else None
                        )
                    debug_log(
                        "mcp_authenticated_proxy.py:315",
                        "MCP server response received",
                        {
                            "method": method,
                            "status": response.status,
                            "response_preview": response_preview,
                            "content_type": response.headers.get("Content-Type"),
                            "has_mcp_session_id": "Mcp-Session-Id"
                            in dict(response.headers),
                        },
                        "A" if method == "GET" else "E",
                    )
                # #endregion

                # Fix: Auto-initialize session if GET /mcp returns 400 "No active transport" with valid token
                if (
                    request.path == "/mcp"
                    and method == "GET"
                    and response.status == 400
                    and provided_token
                    and provided_token in oauth_tokens
                ):
                    try:
                        response_text = (
                            response_body.decode("utf-8") if response_body else ""
                        )
                        if (
                            "No active transport" in response_text
                            or "no active transport" in response_text.lower()
                        ):
                            logger.info(
                                "GET /mcp returned 'No active transport' - auto-initializing session"
                            )
                            # #region agent log
                            debug_log(
                                "mcp_authenticated_proxy.py:350",
                                "Auto-initializing session for GET /mcp",
                                {
                                    "reason": "No active transport error",
                                    "has_token": True,
                                },
                                "A",
                            )
                            # #endregion

                            # Send POST /mcp with initialize to establish session
                            # mcp-proxy creates the session automatically - don't provide session ID
                            init_request = {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "initialize",
                                "params": {
                                    "protocolVersion": "2024-11-05",
                                    "capabilities": {},
                                    "clientInfo": {
                                        "name": "claude-ai",
                                        "version": "1.0.0",
                                    },
                                },
                            }
                            init_headers = {}
                            init_headers["Content-Type"] = "application/json"
                            init_headers[
                                "Accept"
                            ] = "application/json, text/event-stream"  # Required by mcp-proxy
                            # Don't include Authorization header - mcp-proxy doesn't need it (auth is handled by our proxy)
                            # Don't include Mcp-Session-Id - let mcp-proxy create the session

                            async with session.request(
                                method="POST",
                                url=url,
                                headers=init_headers,
                                data=json.dumps(init_request).encode("utf-8"),
                            ) as init_response:
                                init_response_body = await init_response.read()
                                logger.info(
                                    f"Auto-initialize response: {init_response.status}"
                                )

                                # Extract session ID from response headers if present
                                response_headers_dict = dict(init_response.headers)
                                session_id = response_headers_dict.get(
                                    "Mcp-Session-Id"
                                ) or response_headers_dict.get("mcp-session-id")
                                content_type = response_headers_dict.get(
                                    "Content-Type", ""
                                )

                                # #region agent log
                                try:
                                    init_preview = (
                                        init_response_body.decode("utf-8")[:500]
                                        if init_response_body
                                        else None
                                    )
                                except:
                                    init_preview = (
                                        f"<binary {len(init_response_body)} bytes>"
                                        if init_response_body
                                        else None
                                    )
                                debug_log(
                                    "mcp_authenticated_proxy.py:375",
                                    "Auto-initialize response",
                                    {
                                        "status": init_response.status,
                                        "content_type": content_type,
                                        "response_preview": init_preview,
                                        "has_mcp_session_id": bool(session_id),
                                        "session_id": (
                                            session_id[:20] if session_id else None
                                        ),
                                        "all_response_headers": list(
                                            response_headers_dict.keys()
                                        ),
                                        "init_headers_sent": {
                                            k: v
                                            for k, v in init_headers.items()
                                            if k.lower()
                                            in [
                                                "accept",
                                                "content-type",
                                                "authorization",
                                            ]
                                        },
                                    },
                                    "A",
                                )
                                # #endregion

                                # If initialize succeeded (200 with SSE or JSON response), return success for GET /mcp
                                # The session is now established and subsequent requests can use it
                                is_sse_response = (
                                    "text/event-stream" in content_type
                                    or (
                                        init_response_body
                                        and b"event: message" in init_response_body
                                    )
                                )
                                if init_response.status == 200 and (
                                    is_sse_response
                                    or "application/json" in content_type
                                ):
                                    # Return the actual initialize response (SSE or JSON) to Claude.ai
                                    # This allows Claude.ai to see the server capabilities and proceed with the connection
                                    response_headers_clean = {
                                        k: v
                                        for k, v in response_headers_dict.items()
                                        if k.lower() != "transfer-encoding"
                                    }
                                    if session_id:
                                        response_headers_clean[
                                            "Mcp-Session-Id"
                                        ] = session_id
                                    # #region agent log
                                    debug_log(
                                        "mcp_authenticated_proxy.py:425",
                                        "Returning initialize response for GET /mcp",
                                        {
                                            "status": init_response.status,
                                            "content_type": content_type,
                                            "has_session_id": bool(session_id),
                                            "response_length": (
                                                len(init_response_body)
                                                if init_response_body
                                                else 0
                                            ),
                                        },
                                        "A",
                                    )
                                    # #endregion
                                    return web.Response(
                                        body=init_response_body,
                                        status=200,
                                        content_type=content_type,
                                        headers=response_headers_clean,
                                    )
                    except Exception as e:
                        logger.error(
                            f"Error in auto-initialize: {str(e)}", exc_info=True
                        )
                        # Fall through to return original 400 response

                # Filter out Transfer-Encoding header to avoid chunked encoding issues with Cloudflare
                response_headers = {
                    k: v
                    for k, v in dict(response.headers).items()
                    if k.lower() != "transfer-encoding"
                }
                return web.Response(
                    body=response_body,
                    status=response.status,
                    headers=response_headers,
                )
    except ClientError as e:
        logger.error(f"Proxy error for {request.path}: {str(e)}", exc_info=True)
        return web.Response(
            text=f'{{"error": "Proxy error: {str(e)}"}}',
            status=502,
            content_type="application/json",
        )


def get_cors_headers() -> dict:
    """Get CORS headers for browser-based OAuth requests."""
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }


async def handle_oauth_token(
    request: Request, oauth_client_id: str, oauth_client_secret: str
):
    """Handle OAuth 2.0 token endpoint (supports both client_credentials and authorization_code flows)."""
    logger.info(
        f"OAuth token request: {request.method} {request.path_qs} from {request.remote}"
    )

    # #region agent log
    debug_log(
        "mcp_authenticated_proxy.py:256",
        "OAuth token endpoint called",
        {
            "method": request.method,
            "path": request.path,
            "content_type": request.headers.get("Content-Type", ""),
            "origin": request.headers.get("Origin", ""),
        },
        "A",
    )
    # #endregion

    # Handle CORS preflight requests for browser-based OAuth
    if request.method == "OPTIONS":
        # #region agent log
        debug_log(
            "mcp_authenticated_proxy.py:260",
            "CORS preflight request",
            {
                "path": request.path,
                "origin": request.headers.get("Origin", ""),
                "access_control_request_method": request.headers.get(
                    "Access-Control-Request-Method", ""
                ),
                "access_control_request_headers": request.headers.get(
                    "Access-Control-Request-Headers", ""
                ),
            },
            "D",
        )
        # #endregion
        return web.Response(
            headers={
                **get_cors_headers(),
                "Access-Control-Max-Age": "3600",
            },
            status=200,
        )

    # Support both POST form data and JSON
    if request.content_type == "application/json":
        data = await request.json()
    else:
        data = await request.post()

    grant_type = data.get("grant_type", "")
    client_id = data.get("client_id", "")
    client_secret = data.get("client_secret", "")
    logger.info(
        f"OAuth token request - grant_type: {grant_type}, client_id: {client_id[:10]}..."
    )

    # #region agent log
    debug_log(
        "mcp_authenticated_proxy.py:353",
        "OAuth token request parsed",
        {
            "grant_type": grant_type,
            "has_client_id": bool(client_id),
            "has_client_secret": bool(client_secret),
            "has_code": "code" in data,
            "has_code_verifier": "code_verifier" in data,
            "has_refresh_token": "refresh_token" in data,
            "content_type": request.content_type,
        },
        "A",
    )
    # #endregion

    # Handle authorization_code grant type (for OAuth 2.0 authorization code flow)
    if grant_type == "authorization_code":
        code = data.get("code", "")
        redirect_uri = data.get("redirect_uri", "")
        code_verifier = data.get(
            "code_verifier", ""
        )  # PKCE: verifier sent during token exchange
        logger.info(
            f"Authorization code flow - code: {code[:10]}..., redirect_uri: {redirect_uri}, code_verifier: {'present' if code_verifier else 'not provided'}"
        )

        # Validate client_id (always required)
        if client_id != oauth_client_id:
            logger.warning(
                f"Invalid client_id for authorization_code flow: {client_id[:10] if client_id else 'None'}..."
            )
            return web.Response(
                text=json.dumps(
                    {
                        "error": "invalid_client",
                        "error_description": "Invalid client_id",
                    }
                ),
                status=401,
                content_type="application/json",
                headers=get_cors_headers(),
            )

        # For PKCE flows (public clients), client_secret is optional per OAuth 2.0 spec
        # If client_secret is provided, validate it; if not provided, rely on PKCE for security
        if client_secret and client_secret != oauth_client_secret:
            logger.warning("Invalid client_secret for authorization_code flow")
            return web.Response(
                text=json.dumps(
                    {
                        "error": "invalid_client",
                        "error_description": "Invalid client_secret",
                    }
                ),
                status=401,
                content_type="application/json",
                headers=get_cors_headers(),
            )

        if not client_secret:
            logger.info(
                "Client_secret not provided - using PKCE-only validation (public client)"
            )

        # Validate authorization code
        if not code or code not in oauth_codes:
            logger.warning(
                f"Invalid or expired authorization code: {code[:10] if code else 'None'}..."
            )
            logger.debug(f"Available codes: {list(oauth_codes.keys())[:5]}")
            return web.Response(
                text=json.dumps(
                    {
                        "error": "invalid_grant",
                        "error_description": "Invalid or expired authorization code",
                    }
                ),
                status=400,
                content_type="application/json",
                headers=get_cors_headers(),
            )

        # Get code info (state, redirect_uri, PKCE challenge)
        code_info = oauth_codes.get(code)
        if not code_info:
            logger.warning(f"Code info not found for code: {code[:10]}...")
            return web.Response(
                text=json.dumps(
                    {
                        "error": "invalid_grant",
                        "error_description": "Invalid authorization code",
                    }
                ),
                status=400,
                content_type="application/json",
                headers=get_cors_headers(),
            )

        code_info.get("state")
        stored_redirect_uri = code_info.get("redirect_uri")
        stored_code_challenge = code_info.get("code_challenge")
        stored_code_challenge_method = code_info.get("code_challenge_method")
        requested_scope = code_info.get(
            "scope", "claudeai"
        )  # Get requested scope, default to claudeai

        # Validate redirect_uri matches the one used in authorization
        if redirect_uri and stored_redirect_uri and redirect_uri != stored_redirect_uri:
            logger.warning(
                f"Redirect URI mismatch: provided={redirect_uri}, stored={stored_redirect_uri}"
            )
            return web.Response(
                text=json.dumps(
                    {
                        "error": "invalid_grant",
                        "error_description": "redirect_uri mismatch",
                    }
                ),
                status=400,
                content_type="application/json",
                headers=get_cors_headers(),
            )

        # Validate PKCE if challenge was provided during authorization
        if stored_code_challenge:
            if not code_verifier:
                logger.warning(
                    "PKCE challenge was provided but code_verifier is missing"
                )
                return web.Response(
                    text=json.dumps(
                        {
                            "error": "invalid_grant",
                            "error_description": "code_verifier is required for PKCE",
                        }
                    ),
                    status=400,
                    content_type="application/json",
                    headers=get_cors_headers(),
                )

            # Verify PKCE challenge (S256 method)
            if stored_code_challenge_method == "S256":
                import base64
                import hashlib

                code_challenge_verification = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(code_verifier.encode()).digest()
                    )
                    .decode()
                    .rstrip("=")
                )
                if code_challenge_verification != stored_code_challenge:
                    logger.warning("PKCE verification failed: challenge mismatch")
                    return web.Response(
                        text=json.dumps(
                            {
                                "error": "invalid_grant",
                                "error_description": "code_verifier does not match code_challenge",
                            }
                        ),
                        status=400,
                        content_type="application/json",
                        headers=get_cors_headers(),
                    )
                logger.info("PKCE verification successful")
            elif stored_code_challenge_method == "plain":
                if code_verifier != stored_code_challenge:
                    logger.warning("PKCE verification failed: plain challenge mismatch")
                    return web.Response(
                        text=json.dumps(
                            {
                                "error": "invalid_grant",
                                "error_description": "code_verifier does not match code_challenge",
                            }
                        ),
                        status=400,
                        content_type="application/json",
                        headers=get_cors_headers(),
                    )
                logger.info("PKCE verification successful (plain)")

        # Generate access token and refresh token
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        # Include offline_access in scope if not already present (for refresh token support)
        token_scope = requested_scope
        if "offline_access" not in token_scope:
            token_scope = f"{requested_scope} offline_access"

        oauth_tokens[access_token] = {
            "client_id": oauth_client_id,
            "expires_at": None,  # In production, set expiration (e.g., time.time() + 3600)
            "scope": token_scope,
            "refresh_token": refresh_token,  # Store refresh token for token refresh
        }
        # Store refresh token mapping for token refresh flow
        oauth_tokens[refresh_token] = {
            "client_id": oauth_client_id,
            "access_token": access_token,
            "type": "refresh_token",
            "scope": token_scope,
        }
        logger.info(
            f"Generated OAuth access token: {access_token[:10]}... for client {client_id[:10]}... (scope: {token_scope})"
        )

        # Clean up authorization code and state
        oauth_states.pop(code, None)
        oauth_codes.pop(code, None)

        # Return token response (OAuth 2.0 format) with CORS headers for browser-based OAuth
        # Include refresh_token for Claude.ai persistent connections
        # Return the requested scope (with offline_access added for refresh support)
        logger.info("OAuth token exchange successful - access token generated")
        token_response_body = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh_token,
            "scope": token_scope,  # Return requested scope (with offline_access)
        }
        # #region agent log
        debug_log(
            "mcp_authenticated_proxy.py:457",
            "Token response generated",
            {
                "has_access_token": bool(access_token),
                "has_refresh_token": bool(refresh_token),
                "token_type": token_response_body.get("token_type"),
                "expires_in": token_response_body.get("expires_in"),
                "scope": token_response_body.get("scope"),
                "access_token_length": len(access_token) if access_token else 0,
                "refresh_token_length": len(refresh_token) if refresh_token else 0,
            },
            "A",
        )
        # #endregion
        return web.Response(
            text=json.dumps(token_response_body),
            content_type="application/json",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )

    # Handle client_credentials grant type (for direct client authentication)
    elif grant_type == "refresh_token":
        # Handle refresh token grant type (for token refresh)
        refresh_token = data.get("refresh_token", "")
        if not refresh_token or refresh_token not in oauth_tokens:
            return web.Response(
                text=json.dumps(
                    {
                        "error": "invalid_grant",
                        "error_description": "Invalid or expired refresh token",
                    }
                ),
                status=400,
                content_type="application/json",
                headers=get_cors_headers(),
            )

        refresh_token_info = oauth_tokens[refresh_token]
        if refresh_token_info.get("type") != "refresh_token":
            return web.Response(
                text=json.dumps(
                    {
                        "error": "invalid_grant",
                        "error_description": "Invalid refresh token",
                    }
                ),
                status=400,
                content_type="application/json",
                headers=get_cors_headers(),
            )

        # Generate new access token
        old_access_token = refresh_token_info.get("access_token")
        if old_access_token and old_access_token in oauth_tokens:
            oauth_tokens.pop(old_access_token, None)  # Revoke old access token

        new_access_token = secrets.token_urlsafe(32)
        new_refresh_token = secrets.token_urlsafe(32)

        oauth_tokens[new_access_token] = {
            "client_id": refresh_token_info.get("client_id"),
            "expires_at": None,
            "scope": "mcp",
            "refresh_token": new_refresh_token,
        }
        oauth_tokens[new_refresh_token] = {
            "client_id": refresh_token_info.get("client_id"),
            "access_token": new_access_token,
            "type": "refresh_token",
        }
        oauth_tokens.pop(refresh_token, None)  # Revoke old refresh token

        logger.info(f"Token refreshed - new access token: {new_access_token[:10]}...")
        return web.Response(
            text=json.dumps(
                {
                    "access_token": new_access_token,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": new_refresh_token,
                    "scope": "mcp",
                }
            ),
            content_type="application/json",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )

    elif grant_type == "client_credentials":
        # Validate client credentials
        if client_id != oauth_client_id or client_secret != oauth_client_secret:
            return web.Response(
                text=json.dumps(
                    {
                        "error": "invalid_client",
                        "error_description": "Invalid client credentials",
                    }
                ),
                status=401,
                content_type="application/json",
                headers=get_cors_headers(),
            )

        # Generate access token
        access_token = secrets.token_urlsafe(32)
        oauth_tokens[access_token] = {
            "client_id": oauth_client_id,
            "expires_at": None,  # In production, set expiration (e.g., time.time() + 3600)
            "scope": "mcp",
        }

        # Return token response (OAuth 2.0 format) with CORS headers for browser-based OAuth
        return web.Response(
            text=json.dumps(
                {
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "mcp",
                }
            ),
            content_type="application/json",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )

    # Unsupported grant type
    else:
        return web.Response(
            text=json.dumps(
                {
                    "error": "unsupported_grant_type",
                    "error_description": f"Grant type '{grant_type}' is not supported. Supported types: authorization_code, refresh_token, client_credentials",
                }
            ),
            status=400,
            content_type="application/json",
            headers=get_cors_headers(),
        )


async def handle_oauth_authorize(
    request: Request, oauth_client_id: str, oauth_redirect_uri: str
):
    """Handle OAuth 2.0 authorization endpoint (for authorization code flow)."""
    logger.info(
        f"OAuth authorize request: {request.method} {request.path_qs} from {request.remote}"
    )

    # Handle CORS preflight requests (Claude Desktop sends OPTIONS to /authorize)
    if request.method == "OPTIONS":
        return web.Response(
            headers={
                **get_cors_headers(),
                "Access-Control-Max-Age": "3600",
            },
            status=200,
        )

    # Require client_id query parameter
    query_params = request.query_string
    params = parse_qs(query_params)
    provided_client_id = params.get("client_id", [None])[0]
    provided_redirect_uri = params.get("redirect_uri", [oauth_redirect_uri])[0]
    provided_state = params.get("state", [None])[0]
    logger.info(
        f"OAuth authorize - client_id: {provided_client_id[:10] if provided_client_id else 'None'}..., redirect_uri: {provided_redirect_uri}, state: {provided_state[:10] if provided_state else 'None'}..."
    )

    # Validate client_id matches configured client
    if not provided_client_id or provided_client_id != oauth_client_id:
        logger.warning(f"Invalid client_id in authorize request: {provided_client_id}")
        return web.Response(
            text=json.dumps(
                {
                    "error": "invalid_client",
                    "error_description": "Invalid or missing client_id parameter",
                }
            ),
            status=400,
            content_type="application/json",
        )

    # Validate redirect_uri - allow Claude.ai callback or configured redirect URI
    # Claude Desktop uses https://claude.ai/api/mcp/auth_callback as redirect_uri
    allowed_redirect_uris = [
        oauth_redirect_uri,
        "https://claude.ai/api/mcp/auth_callback",  # Claude Desktop standard callback
    ]

    if provided_redirect_uri not in allowed_redirect_uris:
        return web.Response(
            text=json.dumps(
                {
                    "error": "invalid_request",
                    "error_description": f"redirect_uri must be one of: {', '.join(allowed_redirect_uris)}",
                }
            ),
            status=400,
            content_type="application/json",
        )

    # Generate state for CSRF protection (use provided state or generate new)
    state = provided_state or secrets.token_urlsafe(32)
    # Store the provided redirect_uri with the state for later use
    oauth_states[state] = provided_redirect_uri

    # Get PKCE challenge if provided (for PKCE flow)
    code_challenge = params.get("code_challenge", [None])[0]
    code_challenge_method = params.get("code_challenge_method", [None])[0]
    # Get requested scope (Claude.ai requests "claudeai")
    requested_scope = params.get("scope", ["claudeai"])[
        0
    ]  # Default to claudeai if not provided

    # For custom connector, redirect to callback with authorization code
    code = secrets.token_urlsafe(32)
    oauth_states[code] = state  # Store code -> state mapping

    # Store code info with PKCE challenge and scope for token exchange verification
    oauth_codes[code] = {
        "state": state,
        "redirect_uri": provided_redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": requested_scope,  # Store requested scope to return in token response
    }

    if code_challenge:
        logger.info(
            f"PKCE enabled - challenge: {code_challenge[:10]}..., method: {code_challenge_method}"
        )

    # Redirect to the provided redirect_uri (not the configured one)
    # This allows Claude Desktop to use its own callback URL
    callback_url = f"{provided_redirect_uri}?code={code}&state={state}"
    logger.info(f"Redirecting to callback URL: {callback_url}")
    logger.info(
        f"Authorization code generated: {code[:16]}... (stored with PKCE challenge)"
    )
    return web.HTTPFound(location=callback_url)


async def handle_oauth_callback(
    request: Request, oauth_client_id: str, oauth_client_secret: str
):
    """Handle OAuth 2.0 callback endpoint."""
    logger.info(
        f"OAuth callback request: {request.method} {request.path_qs} from {request.remote}"
    )
    query = request.query_string
    params = parse_qs(query)

    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]

    if not code or not state or code not in oauth_states:
        return web.Response(
            text='{"error": "invalid_request", "error_description": "Invalid authorization code or state"}',
            status=400,
            content_type="application/json",
        )

    # Exchange code for access token
    access_token = secrets.token_urlsafe(32)
    oauth_tokens[access_token] = {
        "client_id": oauth_client_id,
        "expires_at": None,
        "scope": "mcp",
    }

    # Clean up state
    oauth_states.pop(code, None)
    oauth_states.pop(state, None)

    # Return token response
    return web.Response(
        text=json.dumps(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "mcp",
            }
        ),
        content_type="application/json",
    )


async def create_app(
    auth_token: Optional[str],
    proxy_url: str,
    oauth_client_id: Optional[str] = None,
    oauth_client_secret: Optional[str] = None,
    oauth_redirect_uri: Optional[str] = None,
):
    """Create aiohttp application with Bearer token or OAuth authentication."""
    app = web.Application()

    # OAuth discovery endpoint (RFC 9728) - must be public (no auth required)
    # Define this OUTSIDE the oauth_client_id block so it's always available
    async def oauth_discovery_handler(request: Request):
        """Handle OAuth protected resource metadata discovery."""
        # Determine base URL from request
        # Check X-Forwarded-Proto header (from Cloudflare tunnel) for correct scheme
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        if scheme not in ("http", "https"):
            scheme = "https"  # Default to HTTPS for public endpoints
        host = request.host
        path = request.path

        # Extract resource identifier from path (e.g., /mcp from /.well-known/oauth-protected-resource/mcp)
        resource_path = ""
        if path.startswith("/.well-known/oauth-protected-resource/"):
            resource_path = path[len("/.well-known/oauth-protected-resource") :]
        elif path == "/.well-known/oauth-protected-resource":
            resource_path = ""

        # Construct resource identifier
        base_url = f"{scheme}://{host}"
        if resource_path:
            resource_identifier = f"{base_url}{resource_path}"
        else:
            resource_identifier = base_url

        # Determine token endpoint URL
        # Claude.ai browser uses /authorize, so return /authorize and /token for compatibility
        # Also support /mcp/oauth/* paths for consistency
        # If resource_path is /mcp, prefer /authorize and /token (what Claude actually uses)
        # but also document /mcp/oauth/* paths for clients that follow the discovery doc exactly
        if resource_path.startswith("/mcp") or resource_path == "/mcp":
            # Return /authorize and /token (what Claude.ai browser actually uses)
            token_endpoint = f"{base_url}/token"
            auth_endpoint = (
                f"{base_url}/authorize"
                if oauth_client_id and oauth_redirect_uri
                else None
            )
        else:
            token_endpoint = f"{base_url}/oauth/token"
            auth_endpoint = (
                f"{base_url}/oauth/authorize"
                if oauth_client_id and oauth_redirect_uri
                else None
            )

        # Build metadata response (RFC 9728)
        # Include offline_access for refresh token support (Claude.ai expects this)
        metadata = {
            "resource": resource_identifier,
            "token_endpoint": token_endpoint,
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["claudeai", "offline_access", "mcp"],
        }

        if auth_endpoint:
            metadata["authorization_endpoint"] = auth_endpoint

        logger.info(
            f"OAuth discovery request: {path} -> resource: {resource_identifier}"
        )

        # #region agent log
        debug_log(
            "mcp_authenticated_proxy.py:790",
            "OAuth discovery response",
            {
                "resource": metadata.get("resource"),
                "token_endpoint": metadata.get("token_endpoint"),
                "authorization_endpoint": metadata.get("authorization_endpoint"),
                "scopes_supported": metadata.get("scopes_supported"),
                "bearer_methods_supported": metadata.get("bearer_methods_supported"),
            },
            "A",
        )
        # #endregion

        return web.Response(
            text=json.dumps(metadata, indent=2),
            content_type="application/json",
        )

    # Register discovery endpoint FIRST (must be before catch-all, always available)
    # Use add_get() for explicit GET method registration with higher priority
    app.router.add_get("/.well-known/oauth-protected-resource", oauth_discovery_handler)
    app.router.add_get(
        "/.well-known/oauth-protected-resource/{path:.*}", oauth_discovery_handler
    )

    # OAuth endpoints (if OAuth is enabled) - MUST be registered before catch-all
    # Support both /oauth/token and /mcp/oauth/token (for path prefix routing)
    if oauth_client_id:
        # Token endpoint (client credentials flow)
        async def oauth_token_handler(request: Request):
            return await handle_oauth_token(
                request, oauth_client_id, oauth_client_secret or ""
            )

        # Register with multiple paths to handle different client behaviors
        # Include OPTIONS for CORS preflight requests
        # /token - matches /authorize pattern (if client derives token endpoint from authorize path)
        app.router.add_route("POST", "/token", oauth_token_handler)
        app.router.add_route("OPTIONS", "/token", oauth_token_handler)
        # /oauth/token - standard OAuth path
        app.router.add_route("POST", "/oauth/token", oauth_token_handler)
        app.router.add_route("OPTIONS", "/oauth/token", oauth_token_handler)
        # /mcp/oauth/token - matches /mcp/oauth/authorize pattern
        app.router.add_route("POST", "/mcp/oauth/token", oauth_token_handler)
        app.router.add_route("OPTIONS", "/mcp/oauth/token", oauth_token_handler)

        # Authorization code flow endpoints (if redirect URI provided)
        if oauth_redirect_uri:

            async def oauth_authorize_handler(request: Request):
                return await handle_oauth_authorize(
                    request, oauth_client_id, oauth_redirect_uri
                )

            async def oauth_callback_handler(request: Request):
                return await handle_oauth_callback(
                    request, oauth_client_id, oauth_client_secret or ""
                )

            # Register /authorize (Claude Desktop compatibility) and /oauth/authorize
            # Include OPTIONS for CORS preflight (Claude Desktop sends OPTIONS to /authorize)
            app.router.add_route("GET", "/authorize", oauth_authorize_handler)
            app.router.add_route("OPTIONS", "/authorize", oauth_authorize_handler)
            app.router.add_route("GET", "/oauth/authorize", oauth_authorize_handler)
            app.router.add_route("OPTIONS", "/oauth/authorize", oauth_authorize_handler)
            app.router.add_route("GET", "/mcp/oauth/authorize", oauth_authorize_handler)
            app.router.add_route(
                "OPTIONS", "/mcp/oauth/authorize", oauth_authorize_handler
            )
            app.router.add_route("GET", "/oauth/callback", oauth_callback_handler)
            app.router.add_route("GET", "/mcp/oauth/callback", oauth_callback_handler)

    # Handle all other routes (MCP requests) - catch-all must be last
    async def handler(request: Request):
        # Skip OAuth endpoints and discovery endpoints (should be handled above, but double-check as fallback)
        path = request.path
        if (
            path.startswith("/oauth/")
            or path.startswith("/mcp/oauth/")
            or path == "/authorize"
            or path == "/token"  # Include /token for OPTIONS preflight support
            or path.startswith("/.well-known/oauth-protected-resource")
        ):
            # This shouldn't happen if routes are registered correctly, but handle it
            if (
                (
                    path == "/token"
                    or path == "/oauth/token"
                    or path == "/mcp/oauth/token"
                )
                and (request.method == "POST" or request.method == "OPTIONS")
                and oauth_client_id
            ):
                return await handle_oauth_token(
                    request, oauth_client_id, oauth_client_secret or ""
                )
            elif (
                (
                    path == "/authorize"
                    or path == "/oauth/authorize"
                    or path == "/mcp/oauth/authorize"
                )
                and request.method == "GET"
                and oauth_redirect_uri
            ):
                return await handle_oauth_authorize(
                    request, oauth_client_id, oauth_redirect_uri
                )
            elif (
                (path == "/oauth/callback" or path == "/mcp/oauth/callback")
                and request.method == "GET"
                and oauth_client_id
            ):
                return await handle_oauth_callback(
                    request, oauth_client_id, oauth_client_secret or ""
                )
            elif (
                path.startswith("/.well-known/oauth-protected-resource")
                and request.method == "GET"
            ):
                # Discovery endpoint fallback (should be handled by registered route, but handle here too)
                return await oauth_discovery_handler(request)
            return web.Response(
                text='{"error": "OAuth endpoint not found"}',
                status=404,
                content_type="application/json",
            )
        return await handle_request(
            request,
            auth_token,
            proxy_url,
            oauth_client_id,
            oauth_client_secret,
            oauth_redirect_uri,
        )

    # Catch-all route (must be registered last so specific routes match first)
    app.router.add_route("*", "/{path:.*}", handler)

    return app


def start_stdio_proxy(server_script: str, stdio_port: int) -> subprocess.Popen:
    """Start the stdio MCP proxy in the background."""
    # Start mcp-proxy stdio-to-http on a different port
    # mcp-proxy syntax: npx mcp-proxy --port <port> --shell python3 <script>
    cmd = [
        "npx",
        "-y",
        "mcp-proxy",
        "--port",
        str(stdio_port),
        "--shell",
        "python3",
        server_script,
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait a moment to check if it started successfully
    import time

    time.sleep(2)

    if process.poll() is not None:
        # Process exited, read error
        _, stderr = process.communicate()
        raise RuntimeError(f"MCP proxy failed to start: {stderr.decode()}")

    return process


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generic authenticated HTTP proxy for MCP Servers"
    )
    parser.add_argument(
        "--server-name",
        required=True,
        help="MCP server name (e.g., 'parquet', 'dnsimple', 'gmail')",
    )
    parser.add_argument(
        "--server-script", required=True, help="Path to MCP server Python script"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PROXY_PORT", DEFAULT_PORT)),
        help=f"HTTP port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_PROXY_HOST", DEFAULT_HOST),
        help=f"HTTP host (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--auth-token",
        help="MCP auth token for authentication (or set MCP_AUTH_TOKEN env var)",
    )
    parser.add_argument(
        "--generate-token",
        action="store_true",
        help="Generate a new auth token and exit",
    )
    parser.add_argument(
        "--stdio-proxy-port",
        type=int,
        default=8081,
        help="Port for internal stdio proxy (default: 8081)",
    )
    parser.add_argument("--oauth-client-id", help="OAuth 2.0 Client ID (for web usage)")
    parser.add_argument(
        "--oauth-client-secret", help="OAuth 2.0 Client Secret (for web usage)"
    )
    parser.add_argument(
        "--oauth-redirect-uri",
        help="OAuth 2.0 Redirect URI (e.g., https://your-tunnel-url.trycloudflare.com/oauth/callback)",
    )

    args = parser.parse_args()

    if not AIOHTTP_AVAILABLE:
        print("Error: aiohttp is required")
        print("Install with: pip install aiohttp")
        sys.exit(1)

    # Generate token if requested
    if args.generate_token:
        token = generate_auth_token()
        print(f"Generated MCP auth token for {args.server_name}: {token}")
        print("\nSave this token securely and use it in client configuration:")
        print(f'  "headers": {{"Authorization": "Bearer {token}"}}')
        print("\nOr set environment variable (MCP standard):")
        print(f'  export MCP_AUTH_TOKEN="{token}"')
        print("\nOr server-specific:")
        print(f'  export MCP_{args.server_name.upper()}_AUTH_TOKEN="{token}"')
        sys.exit(0)

    # Get auth token (if not using OAuth)
    auth_token = None
    if not args.oauth_client_id:
        auth_token = args.auth_token or get_auth_token_from_env(args.server_name)

        if not auth_token:
            print(
                f"Error: MCP auth token is required for {args.server_name} (or use OAuth)"
            )
            print("\nOptions:")
            print("  1. Set MCP_AUTH_TOKEN environment variable (MCP standard, shared)")
            print(
                f"  2. Set MCP_{args.server_name.upper()}_AUTH_TOKEN (server-specific)"
            )
            print("  3. Use --auth-token argument")
            print(
                f"  4. Store in 1Password: '{args.server_name.title()} MCP Proxy' item, 'MCP_AUTH_TOKEN' field"
            )
            print("  5. Generate new token: --generate-token")
            print("  6. Use OAuth: --oauth-client-id and --oauth-client-secret")
            sys.exit(1)

    # Get OAuth credentials from environment variables (.env file synced from 1Password)
    # Credentials should be synced to .env via: python execution/scripts/op_sync_env_from_1password.py
    oauth_client_id = (
        args.oauth_client_id
        or os.environ.get("MCP_OAUTH_CLIENT_ID")
        or os.environ.get(f"MCP_{args.server_name.upper()}_OAUTH_CLIENT_ID")
    )
    oauth_client_secret = (
        args.oauth_client_secret
        or os.environ.get("MCP_OAUTH_CLIENT_SECRET")
        or os.environ.get(f"MCP_{args.server_name.upper()}_OAUTH_CLIENT_SECRET")
    )
    oauth_redirect_uri = (
        args.oauth_redirect_uri
        or os.environ.get("MCP_OAUTH_REDIRECT_URI")
        or os.environ.get(f"MCP_{args.server_name.upper()}_OAUTH_REDIRECT_URI")
    )

    # Validate OAuth configuration if provided
    if oauth_client_id:
        if not oauth_client_secret:
            print("Error: OAuth Client Secret is required when using OAuth")
            print(
                "Set MCP_OAUTH_CLIENT_SECRET environment variable or use --oauth-client-secret"
            )
            sys.exit(1)
        if not oauth_redirect_uri:
            print("Error: --oauth-redirect-uri is required when using OAuth")
            print(
                "Set MCP_OAUTH_REDIRECT_URI environment variable or use --oauth-redirect-uri"
            )
            print(
                "Example: --oauth-redirect-uri https://your-tunnel-url.trycloudflare.com/oauth/callback"
            )
            sys.exit(1)

    # Validate server script exists
    server_script = Path(args.server_script)
    if not server_script.exists():
        print(f"Error: MCP server script not found at {server_script}")
        sys.exit(1)

    # Start stdio proxy in background
    logger.info(
        f"Starting stdio MCP proxy for {args.server_name} on port {args.stdio_proxy_port}..."
    )
    print(
        f"Starting stdio MCP proxy for {args.server_name} on port {args.stdio_proxy_port}..."
    )
    try:
        stdio_proxy_process = start_stdio_proxy(
            str(server_script), args.stdio_proxy_port
        )
        logger.info(
            f"Stdio proxy started successfully (PID: {stdio_proxy_process.pid})"
        )
        print(f"✓ Stdio proxy started (PID: {stdio_proxy_process.pid})")
    except Exception as e:
        logger.error(f"Error starting stdio proxy: {e}", exc_info=True)
        print(f"Error starting stdio proxy: {e}")
        sys.exit(1)

    # Proxy URL for forwarding
    proxy_url = f"http://127.0.0.1:{args.stdio_proxy_port}"

    # Create and start HTTP server
    logger.info(f"Creating HTTP application for {args.server_name}")
    app = await create_app(
        auth_token, proxy_url, oauth_client_id, oauth_client_secret, oauth_redirect_uri
    )
    logger.info(f"HTTP application created, starting server on {args.host}:{args.port}")

    print(f"\n{'='*60}")
    if oauth_client_id:
        logger.info(
            f"Authenticated MCP Proxy: {args.server_name} (OAuth 2.0) - Starting"
        )
        print(f"Authenticated MCP Proxy: {args.server_name} (OAuth 2.0)")
        print(f"{'='*60}")
        print(f"Listening on: http://{args.host}:{args.port}")
        print(f"OAuth Client ID: {oauth_client_id}")
        print(f"OAuth Redirect URI: {oauth_redirect_uri}")
        print("\nOAuth Endpoints:")
        print(f"  Authorization: http://{args.host}:{args.port}/oauth/authorize")
        print(f"  Callback: {oauth_redirect_uri}")
        print("\nFor Custom Connector:")
        print("  URL: https://your-tunnel-url.trycloudflare.com")
        print(f"  OAuth Client ID: {oauth_client_id}")
        print(
            f"  OAuth Client Secret: {oauth_client_secret[:8]}...{oauth_client_secret[-4:] if len(oauth_client_secret) > 12 else '****'} (hidden)"
        )
    else:
        print(f"Authenticated MCP Proxy: {args.server_name} (MCP Standard)")
        print(f"{'='*60}")
        print(f"Listening on: http://{args.host}:{args.port}")
        print(f"Auth Header: {AUTH_HEADER} (Bearer token)")
        print(f"Token: {auth_token[:8]}...{auth_token[-4:]} (hidden)")
        print("\nClient Configuration (MCP Standard):")
        print('  "type": "streamable-http",')
        print(f'  "url": "http://{args.host}:{args.port}",')
        print(f'  "headers": {{"Authorization": "Bearer {auth_token}"}}')
        print("\nOr set environment variable:")
        print(f'  export MCP_AUTH_TOKEN="{auth_token}"')
    print("\nPress Ctrl+C to stop")
    print(f"{'='*60}\n")

    # Cleanup on exit
    def cleanup():
        if stdio_proxy_process.poll() is None:
            stdio_proxy_process.terminate()
            stdio_proxy_process.wait()

    import atexit

    atexit.register(cleanup)

    try:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port)
        await site.start()

        # Keep running
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            print("\nShutting down...")
    finally:
        cleanup()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
