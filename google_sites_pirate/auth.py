import os
import sys
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
OAUTH_PORT = 8080

def get_google_creds(
    credentials_file=None,
    token_file="token.json",
    service_account_path=None,
    non_interactive=False
):
    """
    Build and return Google API credentials.
    Supports both Service Account and User OAuth methods, with environment variable fallbacks.
    """
    # Check if we are running in CI/non-interactive mode
    is_ci = os.environ.get("CI", "").lower() in ("true", "1", "yes")
    is_headless = non_interactive or is_ci or not sys.stdin.isatty()

    # 1. Service Account Authentication (Preferred for non-interactive / CI/CD)
    # Check env var first, then path
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json_str:
        print("[AUTH] Using Google Service Account key from GOOGLE_SERVICE_ACCOUNT_JSON environment variable...")
        try:
            info = json.loads(sa_json_str)
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            raise RuntimeError(f"Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON env var: {e}")

    if service_account_path:
        if not os.path.exists(service_account_path):
            raise FileNotFoundError(f"Service account file not found: {service_account_path}")
        print(f"[AUTH] Using Google Service Account key from file: {service_account_path}...")
        return service_account.Credentials.from_service_account_file(service_account_path, scopes=SCOPES)

    # 2. OAuth Authentication
    # Determine token file content (either from env var or file path)
    creds = None
    token_json_str = os.environ.get("GOOGLE_TOKEN_JSON")
    
    if token_json_str:
        print("[AUTH] Loading OAuth token from GOOGLE_TOKEN_JSON environment variable...")
        try:
            token_info = json.loads(token_json_str)
            creds = Credentials.from_authorized_user_info(token_info, SCOPES)
        except Exception as e:
            print(f"[WARN] Failed to parse GOOGLE_TOKEN_JSON env var: {e}")
    elif token_file and os.path.exists(token_file):
        print(f"[AUTH] Loading OAuth token from file: {token_file}...")
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    # Refresh expired token if possible
    if creds and creds.expired and creds.refresh_token:
        try:
            print("[AUTH] Refreshing expired OAuth token...")
            creds.refresh(Request())
            # Save refreshed token if we loaded from file
            if not token_json_str and token_file:
                with open(token_file, "w") as tf:
                    tf.write(creds.to_json())
            return creds
        except Exception as e:
            print(f"[WARN] Token refresh failed: {e}")
            creds = None

    if creds and creds.valid:
        return creds

    # If no valid creds, we must initialize flow with client secrets
    # Look for client secrets in env var or argument
    creds_json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    flow = None
    
    if creds_json_str:
        print("[AUTH] Loading OAuth client secrets from GOOGLE_CREDENTIALS_JSON environment variable...")
        try:
            client_config = json.loads(creds_json_str)
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        except Exception as e:
            print(f"[WARN] Failed to parse GOOGLE_CREDENTIALS_JSON env var: {e}")
    elif credentials_file and os.path.exists(credentials_file):
        print(f"[AUTH] Loading OAuth client secrets from file: {credentials_file}...")
        flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)

    if not flow:
        # If we couldn't load client secrets, we can't do interactive flow
        raise RuntimeError(
            "No valid Google credentials found. Please provide either:\n"
            "  1. A Google Service Account key via --service-account or GOOGLE_SERVICE_ACCOUNT_JSON env var.\n"
            "  2. A cached OAuth token via GOOGLE_TOKEN_JSON or token.json file.\n"
            "  3. OAuth client secrets via --credentials or GOOGLE_CREDENTIALS_JSON env var to start a new authentication session."
        )

    # Start interactive OAuth flow if allowed
    if is_headless:
        raise RuntimeError(
            "Interactive Google sign-in is required, but current environment is non-interactive/headless.\n"
            "To resolve this, authenticate locally and use the cached token.json, or configure a Google Service Account."
        )

    print("[AUTH] Opening browser for Google sign-in...")
    creds = flow.run_local_server(port=OAUTH_PORT)
    
    # Save the new token to file if requested
    if token_file:
        try:
            with open(token_file, "w") as tf:
                tf.write(creds.to_json())
            print(f"[AUTH] Token cached -> {token_file}")
        except Exception as e:
            print(f"[WARN] Failed to save token.json file: {e}")
            
    return creds
