#!/usr/bin/env python3
"""
Treadmill Strava Upload Module
- OAuth2 authentication with token refresh
- Upload TCX files directly from the Pi
- One-time setup, then fully automatic

Setup:
  1. Go to https://www.strava.com/settings/api
  2. Create an app (any name, localhost as callback)
  3. Run: python3 treadmill_strava.py setup
  4. Follow the instructions
  
Usage:
  python3 treadmill_strava.py upload <file.tcx>
  python3 treadmill_strava.py upload-latest
"""

import json
import os
import time
import glob
import requests

CONFIG_DIR = os.path.expanduser("~")
STRAVA_TOKEN_FILE = os.path.join(CONFIG_DIR, "strava_token.json")
STRAVA_CONFIG_FILE = os.path.join(CONFIG_DIR, "strava_config.json")
EXPORT_DIR = os.path.expanduser("~/treadmill_exports")

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_UPLOAD_URL = "https://www.strava.com/api/v3/uploads"


# ==================================
# Config
# ==================================
def load_strava_config():
    if os.path.exists(STRAVA_CONFIG_FILE):
        with open(STRAVA_CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_strava_config(config):
    with open(STRAVA_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def load_token():
    if os.path.exists(STRAVA_TOKEN_FILE):
        with open(STRAVA_TOKEN_FILE, "r") as f:
            return json.load(f)
    return None

def save_token(token):
    with open(STRAVA_TOKEN_FILE, "w") as f:
        json.dump(token, f, indent=2)


# ==================================
# OAuth2 Authentication
# ==================================
def setup():
    """One-time interactive setup."""
    print("=== Configuration Strava API ===\n")
    print("1. Va sur https://www.strava.com/settings/api")
    print("2. Cree une application:")
    print("   - Application Name: Treadmill Sensor")
    print("   - Category: Training")
    print("   - Club: (vide)")
    print("   - Website: http://localhost")
    print("   - Authorization Callback Domain: localhost")
    print("3. Note le Client ID et Client Secret\n")

    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()

    config = {"client_id": client_id, "client_secret": client_secret}
    save_strava_config(config)

    # Generate auth URL
    auth_url = (
        f"{STRAVA_AUTH_URL}?"
        f"client_id={client_id}"
        f"&response_type=code"
        f"&redirect_uri=http://localhost"
        f"&approval_prompt=force"
        f"&scope=activity:write,activity:read"
    )

    print(f"\n4. Ouvre cette URL dans ton navigateur:\n")
    print(f"   {auth_url}\n")
    print("5. Autorise l'application sur Strava")
    print("6. Tu seras redirige vers localhost avec une erreur - c'est NORMAL")
    print("7. Copie le 'code' dans l'URL:")
    print("   http://localhost/?state=&code=XXXXXXX&scope=...")
    print("   Le code c'est la partie entre code= et &scope\n")

    code = input("Code: ").strip()

    # Exchange code for tokens
    print("\nEchange du code pour les tokens...")
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code"
    })

    if resp.status_code != 200:
        print(f"Erreur: {resp.status_code} - {resp.text}")
        return False

    token = resp.json()
    save_token(token)

    athlete = token.get("athlete", {})
    print(f"\nConnecte en tant que: {athlete.get('firstname', '?')} {athlete.get('lastname', '?')}")
    print(f"Token expire dans: {token.get('expires_in', 0) // 3600}h")
    print("\nSetup termine! Les uploads seront automatiques.")
    return True


def refresh_token_if_needed():
    """Refresh access token if expired. Returns valid access_token or None."""
    token = load_token()
    if not token:
        return None

    config = load_strava_config()
    if not config.get("client_id"):
        return None

    # Check if expired (with 5 min margin)
    if token.get("expires_at", 0) > time.time() + 300:
        return token["access_token"]

    # Refresh
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token"
    })

    if resp.status_code != 200:
        print(f"[STRAVA] Token refresh failed: {resp.status_code}")
        return None

    new_token = resp.json()
    # Preserve athlete info from old token
    if "athlete" not in new_token and "athlete" in token:
        new_token["athlete"] = token["athlete"]
    save_token(new_token)
    return new_token["access_token"]


def is_configured():
    """Check if Strava is set up."""
    return os.path.exists(STRAVA_TOKEN_FILE) and os.path.exists(STRAVA_CONFIG_FILE)


# ==================================
# Upload
# ==================================
def upload_tcx(tcx_path, name=None, description=None):
    """
    Upload a TCX file to Strava.
    Returns (success, message).
    """
    access_token = refresh_token_if_needed()
    if not access_token:
        return False, "Non authentifie - lance: python3 treadmill_strava.py setup"

    if not os.path.exists(tcx_path):
        return False, f"Fichier non trouve: {tcx_path}"

    if name is None:
        basename = os.path.basename(tcx_path).replace(".tcx", "").replace("run_", "")
        name = f"Treadmill Run {basename}"

    if description is None:
        description = "Uploaded from Treadmill Sensor"

    headers = {"Authorization": f"Bearer {access_token}"}

    with open(tcx_path, "rb") as f:
        resp = requests.post(
            STRAVA_UPLOAD_URL,
            headers=headers,
            files={"file": (os.path.basename(tcx_path), f, "application/xml")},
            data={
                "data_type": "tcx",
                "name": name,
                "description": description,
            }
        )

    if resp.status_code not in (200, 201):
        return False, f"Upload failed: {resp.status_code} - {resp.text}"

    result = resp.json()
    upload_id = result.get("id")

    # Poll for processing status
    for _ in range(10):
        time.sleep(2)
        check = requests.get(
            f"{STRAVA_UPLOAD_URL}/{upload_id}",
            headers=headers
        )
        if check.status_code == 200:
            status = check.json()
            if status.get("activity_id"):
                activity_id = status["activity_id"]
                return True, f"OK! strava.com/activities/{activity_id}"
            if status.get("error"):
                return False, f"Strava: {status['error']}"
            # Still processing
        else:
            break

    return True, f"Upload envoye (id={upload_id}), verification en cours..."


def upload_latest():
    """Upload the most recent TCX file."""
    files = sorted(glob.glob(os.path.join(EXPORT_DIR, "run_*.tcx")),
                   key=os.path.getmtime, reverse=True)
    if not files:
        return False, "Aucun fichier TCX dans ~/treadmill_exports/"
    return upload_tcx(files[0])


# ==================================
# CLI
# ==================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 treadmill_strava.py setup          # Configuration initiale")
        print("  python3 treadmill_strava.py upload <file>   # Upload un fichier TCX")
        print("  python3 treadmill_strava.py upload-latest   # Upload le dernier TCX")
        print("  python3 treadmill_strava.py status          # Verifier la connexion")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "setup":
        setup()

    elif cmd == "upload":
        if len(sys.argv) < 3:
            print("Usage: python3 treadmill_strava.py upload <fichier.tcx>")
            sys.exit(1)
        ok, msg = upload_tcx(sys.argv[2])
        print(f"{'OK' if ok else 'ERREUR'}: {msg}")

    elif cmd == "upload-latest":
        ok, msg = upload_latest()
        print(f"{'OK' if ok else 'ERREUR'}: {msg}")

    elif cmd == "status":
        if not is_configured():
            print("Strava non configure. Lance: python3 treadmill_strava.py setup")
        else:
            token = refresh_token_if_needed()
            if token:
                t = load_token()
                athlete = t.get("athlete", {})
                name = f"{athlete.get('firstname', '?')} {athlete.get('lastname', '?')}"
                expires = t.get("expires_at", 0) - time.time()
                print(f"Connecte: {name}")
                print(f"Token valide pour: {int(expires // 3600)}h {int((expires % 3600) // 60)}min")
            else:
                print("Token expire, relance: python3 treadmill_strava.py setup")
    else:
        print(f"Commande inconnue: {cmd}")
