"""
Verify Coinbase Advanced Trade API credentials are a matching pair.

This script tests authentication with a simple endpoint to verify
the API key and secret are correctly paired.
"""

import base64
import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    repo_root = Path(__file__).parent.parent
    dotenv_path = repo_root / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path)
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
    from cryptography.hazmat.primitives.asymmetric import ec

    ECDSA_AVAILABLE = True
except ImportError:
    ECDSA_AVAILABLE = False

try:
    import jwt

    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False


def sign_request_hmac(
    secret: str,
    timestamp: str,
    method: str,
    path: str,
    body: str = "",
    use_raw_secret: bool = False,
) -> str:
    """HMAC-SHA256 signing for Coinbase Advanced Trade API."""
    if use_raw_secret:
        # Use secret as-is (base64 string) without decoding
        secret_bytes = secret.encode("utf-8")
    else:
        try:
            secret_bytes = base64.b64decode(secret)
        except Exception:
            secret_bytes = secret.encode("utf-8")

    message = f"{timestamp}{method}{path}{body}".encode()
    signature = hmac.new(secret_bytes, message, hashlib.sha256).digest()
    return base64.b64encode(signature).decode("utf-8")


def sign_request_ecdsa(
    secret: str, timestamp: str, method: str, path: str, body: str = ""
) -> str:
    """ECDSA signing for Coinbase Advanced Trade API (P-256 curve)."""
    if not ECDSA_AVAILABLE:
        raise RuntimeError(
            "cryptography library required. Install: pip install cryptography"
        )

    # Handle PEM format (starts with "-----BEGIN")
    if secret.strip().startswith("-----BEGIN"):
        # PEM format - use as-is
        try:
            private_key = serialization.load_pem_private_key(
                secret.encode("utf-8"), password=None, backend=default_backend()
            )
        except Exception as e:
            raise RuntimeError(f"Failed to load PEM private key: {e}") from e
    else:
        # Try base64-decoded DER format
        try:
            secret_bytes = base64.b64decode(secret)
            # Try DER first
            try:
                private_key = serialization.load_der_private_key(
                    secret_bytes, password=None, backend=default_backend()
                )
            except Exception:
                # If not DER, try PEM (might be base64-encoded PEM)
                try:
                    private_key = serialization.load_pem_private_key(
                        secret_bytes, password=None, backend=default_backend()
                    )
                except Exception as e:
                    raise ValueError(
                        f"Secret format not recognized for ECDSA: {e}"
                    ) from e
        except Exception as e:
            raise RuntimeError(f"Failed to decode secret: {e}") from e

    # Verify it's an EC key
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError("Key is not an ECDSA private key")

    # Build message: timestamp + method + path + body
    message = f"{timestamp}{method}{path}{body}".encode()

    # Sign with ECDSA (P-256)
    signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))

    # Return base64-encoded signature
    return base64.b64encode(signature).decode("utf-8")


def sign_request_jwt(
    api_key: str, secret: str, method: str, path: str, body: str = ""
) -> str:
    """JWT signing for Coinbase Cloud/Prime API."""
    if not JWT_AVAILABLE or not ECDSA_AVAILABLE:
        raise RuntimeError(
            "jwt and cryptography libraries required. Install: pip install pyjwt cryptography"
        )

    # Load ECDSA private key from PEM
    if secret.strip().startswith("-----BEGIN"):
        try:
            private_key = serialization.load_pem_private_key(
                secret.encode("utf-8"), password=None, backend=default_backend()
            )
        except Exception as e:
            raise RuntimeError(f"Failed to load PEM private key: {e}") from e
    else:
        raise ValueError("JWT requires PEM format private key")

    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError("Key is not an ECDSA private key")

    # Build JWT payload for Coinbase Cloud API
    now = int(time.time())
    # Try different payload formats
    payload = {
        "sub": api_key,
        "iss": "coinbase-cloud",
        "nbf": now,
        "exp": now + 120,  # 2 minute expiry
        "aud": ["retail_rest_api_proxy"],
        "uri": path,  # Some APIs require the URI in the JWT
    }

    # Sign JWT with ECDSA P-256 (ES256)
    token = jwt.encode(payload, private_key, algorithm="ES256")
    return token


def sign_request_ed25519(
    secret: str, timestamp: str, method: str, path: str, body: str = ""
) -> str:
    """Ed25519 signing (for Coinbase Cloud API, not Advanced Trade)."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        raise RuntimeError(
            "cryptography library required. Install: pip install cryptography"
        )

    # Decode base64 secret to get Ed25519 private key
    try:
        secret_bytes = base64.b64decode(secret)
        # Ed25519 private key is 32 bytes
        # If secret is 64 bytes, use first 32 bytes (private key)
        # If secret is 32 bytes, use as-is
        if len(secret_bytes) == 64:
            private_key_bytes = secret_bytes[:32]
        elif len(secret_bytes) == 32:
            private_key_bytes = secret_bytes
        else:
            raise ValueError(
                f"Unexpected secret length: {len(secret_bytes)} bytes (expected 32 or 64)"
            )

        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    except Exception as e:
        raise RuntimeError(f"Failed to load Ed25519 private key: {e}") from e

    # Build message: timestamp + method + path + body
    message = f"{timestamp}{method}{path}{body}".encode()

    # Sign with Ed25519
    signature_bytes = private_key.sign(message)

    # Return base64-encoded signature
    return base64.b64encode(signature_bytes).decode("utf-8")


def test_credentials(
    api_key: str,
    api_secret: str,
    api_passphrase: str = "",
    method: str = "hmac",
    use_cloud_endpoint: bool = False,
) -> bool:
    """Test if credentials work by calling a simple endpoint."""
    # Test with products endpoint (read-only, no special permissions needed)
    if use_cloud_endpoint:
        # Coinbase Cloud API might use different base URL
        path = "/v1/products/STX-USDC"
        url = f"https://api.coinbase.com{path}"
    else:
        path = "/api/v3/brokerage/products/STX-USDC"
        url = f"https://api.coinbase.com{path}"

    timestamp = str(int(time.time()))
    http_method = "GET"
    body = ""

    # Check if this is a Cloud API key (starts with "organizations/")
    api_key.startswith("organizations/")

    # Try different signing methods
    if method == "jwt":
        token = sign_request_jwt(api_key, api_secret, http_method, path, body)
        method_name = "JWT (ES256)"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    elif method == "hmac":
        signature = sign_request_hmac(
            api_secret, timestamp, http_method, path, body, use_raw_secret=False
        )
        method_name = "HMAC-SHA256"
        headers = {
            "CB-ACCESS-KEY": api_key,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }
        if api_passphrase:
            headers["CB-ACCESS-PASSPHRASE"] = api_passphrase
    elif method == "hmac_raw":
        signature = sign_request_hmac(
            api_secret, timestamp, http_method, path, body, use_raw_secret=True
        )
        method_name = "HMAC-SHA256 (raw secret)"
        headers = {
            "CB-ACCESS-KEY": api_key,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }
        if api_passphrase:
            headers["CB-ACCESS-PASSPHRASE"] = api_passphrase
    elif method == "ecdsa":
        signature = sign_request_ecdsa(api_secret, timestamp, http_method, path, body)
        method_name = "ECDSA"
        headers = {
            "CB-ACCESS-KEY": api_key,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }
        if api_passphrase:
            headers["CB-ACCESS-PASSPHRASE"] = api_passphrase
    elif method == "ed25519":
        signature = sign_request_ed25519(api_secret, timestamp, http_method, path, body)
        method_name = "Ed25519"
        headers = {
            "CB-ACCESS-KEY": api_key,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }
        if api_passphrase:
            headers["CB-ACCESS-PASSPHRASE"] = api_passphrase
    else:
        raise ValueError(f"Unknown method: {method}")

    # Debug: show what we're signing
    if method == "jwt":
        print(f"\nDEBUG ({method_name}):")
        print(f"  API Key: {api_key[:30]}...")
        print(f"  Path: {path}")
        print(f"  Method: {http_method}")
        print(f"  Token (first 50 chars): {token[:50]}...")
    else:
        message = f"{timestamp}{http_method}{path}{body}"
        print(f"\nDEBUG ({method_name}):")
        print(f"  Message to sign: {message}")
        print(f"  Path: {path}")
        print(f"  Method: {http_method}")
        print(f"  Timestamp: {timestamp}")
        print(f"  Signature (first 20 chars): {signature[:20]}...")

    print(f"  Headers: {', '.join(headers.keys())}")
    print()

    try:
        resp = requests.get(url, headers=headers, timeout=10)

        print(f"Status Code: {resp.status_code}")
        print(f"Response: {resp.text[:200]}")

        if resp.status_code == 200:
            print(f"\n✓ SUCCESS: Credentials are valid with {method_name}!")
            return True
        elif resp.status_code == 401:
            print(f"\n✗ FAILED: 401 Unauthorized with {method_name}")
            return False
        else:
            print(f"\n✗ FAILED: Unexpected status code {resp.status_code}")
            return False

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        return False


def main():
    print("Verifying Coinbase Advanced Trade API Credentials\n")
    print("=" * 72)

    # Get credentials
    api_key = os.getenv("COINBASE_API_KEY_ADVANCED")
    api_secret = os.getenv("COINBASE_API_SECRET_ADVANCED")
    api_passphrase = os.getenv("COINBASE_API_PASSPHRASE", "")

    if not api_key or not api_secret:
        print(
            "ERROR: COINBASE_API_KEY_ADVANCED or COINBASE_API_SECRET_ADVANCED not set"
        )
        print("\nSet them in .env file or environment variables")
        return 1

    print(f"API Key: {api_key[:20]}...{api_key[-10:]}")
    print(f"Secret: {'SET' if api_secret else 'NOT SET'} ({len(api_secret)} chars)")
    print(f"Passphrase: {'SET' if api_passphrase else 'NOT SET'}")
    print()

    # Check if this is a Cloud API key
    is_cloud_api = api_key.startswith("organizations/")

    if is_cloud_api:
        print("Detected Coinbase Cloud API key format (organizations/...)")
        print("Testing with JWT (ES256) - Advanced Trade endpoint...")
        try:
            success = test_credentials(
                api_key,
                api_secret,
                api_passphrase,
                method="jwt",
                use_cloud_endpoint=False,
            )
        except Exception as e:
            print(f"  JWT failed: {e}")
            success = False

        if not success:
            print("\n" + "=" * 72)
            print("Testing with JWT (ES256) - Cloud API endpoint...")
            try:
                success = test_credentials(
                    api_key,
                    api_secret,
                    api_passphrase,
                    method="jwt",
                    use_cloud_endpoint=True,
                )
            except Exception as e:
                print(f"  JWT (cloud endpoint) failed: {e}")
                success = False
    else:
        # Advanced Trade API - try ECDSA first
        print("Testing with ECDSA (required for Advanced Trade)...")
        try:
            success = test_credentials(
                api_key, api_secret, api_passphrase, method="ecdsa"
            )
        except Exception as e:
            print(f"  ECDSA failed: {e}")
            success = False

        if not success:
            print("\n" + "=" * 72)
            print("Testing with HMAC-SHA256 (decoded secret)...")
            success = test_credentials(
                api_key, api_secret, api_passphrase, method="hmac"
            )

        if not success:
            print("\n" + "=" * 72)
            print("Testing with HMAC-SHA256 (raw secret string)...")
            success = test_credentials(
                api_key, api_secret, api_passphrase, method="hmac_raw"
            )

    print("\n" + "=" * 72)
    if success:
        print("Credentials verified! You can now fetch orders.")
        return 0
    else:
        print("Credentials verification failed. Please check:")
        print("  1. API key and secret are from the same Coinbase account")
        print("  2. API key has 'view' permission enabled")
        print("  3. API key is active (not revoked)")
        print("  4. You're using Advanced Trade API credentials (not Consumer API)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
