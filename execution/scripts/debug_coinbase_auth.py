"""
Debug Coinbase API authentication.

This script systematically tests different authentication methods and API endpoints
to identify which combination works with the available credentials.
"""

import base64
import hashlib
import hmac
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    repo_root = Path(__file__).parent.parent
    dotenv_path = repo_root / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path)
        print(f"Loaded environment variables from {dotenv_path}")
except ImportError:
    pass

try:
    import requests
except ImportError:
    print("ERROR: requests module not installed.")
    sys.exit(1)

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    print("WARNING: cryptography not available. Some methods will be skipped.")

try:
    import jwt

    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False

# 1Password configuration
OP_VAULT = "Wallets"
OP_ITEM = "gstd7jfxcadhjuwjo2enfwssiq"
OP_FIELD_API_KEY = "API key ID / name"
OP_FIELD_API_SECRET = "API private key"
OP_FIELD_API_PASSPHRASE = "Valor de inicialización secreto"


def op_read(field_ref: str) -> str:
    """Read a secret from 1Password using the op CLI."""
    try:
        result = subprocess.run(
            ["op", "read", field_ref],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception as e:
        raise RuntimeError(f"1Password CLI error for {field_ref}: {e}") from e


def get_credentials() -> dict[str, str]:
    """Get credentials from environment or 1Password."""
    # Try environment first
    key = os.getenv("COINBASE_API_KEY") or os.getenv("COINBASE_API_KEY_ADVANCED")
    secret = os.getenv("COINBASE_API_SECRET") or os.getenv(
        "COINBASE_API_SECRET_ADVANCED"
    )
    passphrase = os.getenv("COINBASE_API_PASSPHRASE", "")

    if not key or not secret:
        print("Credentials not in environment, trying 1Password...")
        try:
            key = op_read(f"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_KEY}")
            secret = op_read(f"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_SECRET}")
            try:
                passphrase = op_read(
                    f"op://{OP_VAULT}/{OP_ITEM}/{OP_FIELD_API_PASSPHRASE}"
                )
            except Exception:
                passphrase = ""
            print("✓ Retrieved credentials from 1Password")
        except Exception as e:
            print(f"✗ Failed to get credentials from 1Password: {e}")
            sys.exit(1)
    else:
        print("✓ Using credentials from environment")

    return {"key": key, "secret": secret, "passphrase": passphrase}


def analyze_credentials(key: str, secret: str) -> dict[str, any]:
    """Analyze credential format to determine API type."""
    analysis = {
        "key_format": "unknown",
        "secret_format": "unknown",
        "key_length": len(key),
        "secret_length": len(secret),
        "key_preview": key[:30] + "..." if len(key) > 30 else key,
        "secret_preview": secret[:50] + "..." if len(secret) > 50 else secret,
    }

    # Analyze key
    if key.startswith("organizations/"):
        analysis["key_format"] = "coinbase_cloud"
    elif len(key) == 36 and "-" in key:  # UUID format
        analysis["key_format"] = "uuid_format"
    elif len(key) < 20:
        analysis["key_format"] = "short_key"
    else:
        analysis["key_format"] = "unknown_format"

    # Analyze secret
    if secret.strip().startswith("-----BEGIN"):
        analysis["secret_format"] = "pem_format"
        if "PRIVATE KEY" in secret:
            if "EC PRIVATE KEY" in secret:
                analysis["secret_format"] = "pem_ec_private_key"
            elif "RSA PRIVATE KEY" in secret:
                analysis["secret_format"] = "pem_rsa_private_key"
            else:
                analysis["secret_format"] = "pem_private_key"
    else:
        # Try to decode as base64
        try:
            decoded = base64.b64decode(secret)
            if len(decoded) == 32:
                analysis["secret_format"] = "base64_ed25519_32"
            elif len(decoded) == 64:
                analysis["secret_format"] = "base64_ed25519_64"
            else:
                analysis["secret_format"] = "base64_unknown"
        except Exception:
            analysis["secret_format"] = "raw_string"

    return analysis


def sign_hmac(
    secret: str, timestamp: str, method: str, path: str, body: str = ""
) -> str:
    """HMAC-SHA256 signing."""
    message = f"{timestamp}{method}{path}{body}".encode()
    try:
        # Try base64 decode first
        secret_bytes = base64.b64decode(secret)
    except Exception:
        # Use as raw string
        secret_bytes = secret.encode("utf-8")

    sig = hmac.new(secret_bytes, message, hashlib.sha256).digest()
    return base64.b64encode(sig).decode("utf-8")


def sign_ecdsa(
    secret: str, timestamp: str, method: str, path: str, body: str = ""
) -> str:
    """ECDSA signing (P-256 curve)."""
    if not CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library required")

    # Load PEM private key
    secret_str = secret.strip()

    # Check if it's already PEM format
    if secret_str.startswith("-----BEGIN"):
        # Replace literal \n with actual newlines if needed
        secret_str = secret_str.replace("\\n", "\n")
        try:
            private_key = serialization.load_pem_private_key(
                secret_str.encode("utf-8"), password=None, backend=default_backend()
            )
        except Exception:
            # If PEM load fails, try treating as base64-encoded
            try:
                decoded = base64.b64decode(secret_str)
                private_key = serialization.load_der_private_key(
                    decoded, password=None, backend=default_backend()
                )
            except Exception as e:
                raise ValueError(f"Cannot load ECDSA key from PEM format: {e}")
    else:
        # Try base64 decoding - might be base64-encoded PEM or DER
        try:
            decoded = base64.b64decode(secret_str)
            # First try as DER (binary format)
            try:
                private_key = serialization.load_der_private_key(
                    decoded, password=None, backend=default_backend()
                )
            except Exception:
                # If DER fails, try as PEM string
                decoded_str = decoded.decode("utf-8", errors="ignore")
                decoded_str = decoded_str.replace("\\n", "\n")
                if decoded_str.strip().startswith("-----BEGIN"):
                    private_key = serialization.load_pem_private_key(
                        decoded_str.encode("utf-8"),
                        password=None,
                        backend=default_backend(),
                    )
                else:
                    raise ValueError("Decoded secret is neither DER nor PEM format")
        except Exception as e:
            raise ValueError(f"Cannot load ECDSA key from secret format: {e}")

    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError("Key is not an ECDSA private key")

    message = f"{timestamp}{method}{path}{body}".encode()
    signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode("utf-8")


def sign_ed25519(
    secret: str, timestamp: str, method: str, path: str, body: str = ""
) -> str:
    """Ed25519 signing."""
    if not CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library required")

    try:
        secret_bytes = base64.b64decode(secret)
        if len(secret_bytes) == 64:
            private_key_bytes = secret_bytes[:32]
        elif len(secret_bytes) == 32:
            private_key_bytes = secret_bytes
        else:
            raise ValueError(f"Unexpected secret length: {len(secret_bytes)} bytes")

        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    except Exception as e:
        raise RuntimeError(f"Failed to load Ed25519 key: {e}") from e

    message = f"{timestamp}{method}{path}{body}".encode()
    signature_bytes = private_key.sign(message)
    return base64.b64encode(signature_bytes).decode("utf-8")


def sign_jwt(key: str, secret: str, method: str, path: str) -> str:
    """JWT signing for Coinbase Cloud API."""
    if not JWT_AVAILABLE or not CRYPTO_AVAILABLE:
        raise RuntimeError("jwt and cryptography libraries required")

    if not secret.strip().startswith("-----BEGIN"):
        raise ValueError("JWT requires PEM format private key")

    private_key = serialization.load_pem_private_key(
        secret.encode("utf-8"), password=None, backend=default_backend()
    )

    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError("Key is not an ECDSA private key")

    now = int(time.time())
    payload = {
        "sub": key,
        "iss": "coinbase-cloud",
        "nbf": now,
        "exp": now + 120,
        "aud": ["retail_rest_api_proxy"],
    }

    token = jwt.encode(payload, private_key, algorithm="ES256")
    return token


def test_endpoint(
    key: str,
    secret: str,
    passphrase: str,
    url: str,
    path: str,
    method: str = "GET",
    sign_method: str = "hmac",
    use_jwt: bool = False,
) -> tuple[bool, str, int]:
    """Test an endpoint with given authentication method."""
    timestamp = str(int(time.time()))
    body = ""

    try:
        if use_jwt:
            token = sign_jwt(key, secret, method, path)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        else:
            if sign_method == "hmac":
                signature = sign_hmac(secret, timestamp, method, path, body)
            elif sign_method == "ecdsa":
                signature = sign_ecdsa(secret, timestamp, method, path, body)
            elif sign_method == "ed25519":
                signature = sign_ed25519(secret, timestamp, method, path, body)
            else:
                return False, f"Unknown sign method: {sign_method}", 0

            headers = {
                "CB-ACCESS-KEY": key,
                "CB-ACCESS-SIGN": signature,
                "CB-ACCESS-TIMESTAMP": timestamp,
                "Content-Type": "application/json",
            }
            if passphrase:
                headers["CB-ACCESS-PASSPHRASE"] = passphrase

        resp = requests.get(url, headers=headers, timeout=10)
        return resp.status_code == 200, resp.text[:500], resp.status_code

    except Exception as e:
        return False, str(e), 0


def main():
    print("=" * 80)
    print("COINBASE API AUTHENTICATION DEBUGGER")
    print("=" * 80)
    print()

    # Get credentials
    creds = get_credentials()
    key = creds["key"]
    secret = creds["secret"]
    passphrase = creds["passphrase"]

    # Analyze credentials
    print("CREDENTIAL ANALYSIS")
    print("-" * 80)
    analysis = analyze_credentials(key, secret)
    for k, v in analysis.items():
        print(f"  {k}: {v}")
    print()

    # Test endpoints
    endpoints = [
        # Consumer API v2
        {
            "name": "Consumer API v2 - Accounts",
            "url": "https://api.coinbase.com/v2/accounts",
            "path": "/v2/accounts",
            "methods": ["hmac"],
        },
        # Advanced Trade API
        {
            "name": "Advanced Trade API - Products",
            "url": "https://api.coinbase.com/api/v3/brokerage/products/STX-USDC",
            "path": "/api/v3/brokerage/products/STX-USDC",
            "methods": ["ecdsa", "ed25519", "hmac"],
        },
        {
            "name": "Advanced Trade API - Accounts",
            "url": "https://api.coinbase.com/api/v3/brokerage/accounts",
            "path": "/api/v3/brokerage/accounts",
            "methods": ["ecdsa", "ed25519", "hmac"],
        },
        {
            "name": "Advanced Trade API - Orders",
            "url": "https://api.coinbase.com/api/v3/brokerage/orders/historical/batch",
            "path": "/api/v3/brokerage/orders/historical/batch",
            "methods": ["ecdsa", "ed25519", "hmac"],
        },
        # Cloud API (JWT)
        {
            "name": "Cloud API (JWT) - Products",
            "url": "https://api.coinbase.com/api/v3/brokerage/products/STX-USDC",
            "path": "/api/v3/brokerage/products/STX-USDC",
            "methods": ["jwt"],
            "use_jwt": True,
        },
    ]

    print("TESTING ENDPOINTS")
    print("-" * 80)

    success_count = 0
    for endpoint in endpoints:
        print(f"\n{endpoint['name']}")
        print(f"  URL: {endpoint['url']}")

        for method in endpoint.get("methods", ["hmac"]):
            use_jwt = endpoint.get("use_jwt", False)
            if use_jwt and method != "jwt":
                continue
            if method == "jwt" and not use_jwt:
                continue

            try:
                success, response, status = test_endpoint(
                    key,
                    secret,
                    passphrase,
                    endpoint["url"],
                    endpoint["path"],
                    sign_method=method,
                    use_jwt=use_jwt,
                )

                if success:
                    print(f"  ✓ {method.upper()}: SUCCESS (200 OK)")
                    print(f"    Response: {response[:100]}...")
                    success_count += 1
                    break  # Found working method, no need to try others
                else:
                    print(f"  ✗ {method.upper()}: FAILED ({status})")
                    if status == 401:
                        print("    Error: Unauthorized - authentication failed")
                    elif status == 403:
                        print("    Error: Forbidden - insufficient permissions")
                    else:
                        print(f"    Response: {response[:200]}")
            except Exception as e:
                print(f"  ✗ {method.upper()}: ERROR - {e}")

    print()
    print("=" * 80)
    if success_count > 0:
        print(f"✓ FOUND {success_count} WORKING ENDPOINT(S)")
        print(
            "\nRecommendation: Use the working endpoint and authentication method above."
        )
    else:
        print("✗ NO WORKING ENDPOINTS FOUND")
        print("\nPossible issues:")
        print("  1. API key may be expired or revoked")
        print("  2. API key may be for wrong API type (Consumer vs Advanced Trade)")
        print("  3. Secret format may not match expected format")
        print("  4. Permissions may be insufficient")
        print("\nNext steps:")
        print("  1. Verify API key is active in Coinbase settings")
        print("  2. Check API key permissions (view, trade, etc.)")
        print("  3. Generate new API key if needed")
        print(
            "  4. Ensure you're using Advanced Trade API credentials (not Consumer API)"
        )

    print("=" * 80)
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
