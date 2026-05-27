"""
Setup OAuth token for Google Drive API v3.
Usage:
  .venv\\Scripts\\python.exe scripts\\setup_gdrive_token.py

Requires:
  data/gdrive_oauth_credentials.json   (OAuth client: Desktop app)
Creates/updates:
  data/gdrive_token.json              (authorized user credentials)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Avoid importing/booting the Flask app when importing app.* utilities.
os.environ["ANDES_SKIP_AUTO_CREATE_APP"] = "1"

from app.utils.load_env import load_project_dotenv


def main() -> int:
    load_project_dotenv()

    from google_auth_oauthlib.flow import InstalledAppFlow

    oauth_path = (os.environ.get("GDRIVE_OAUTH_CREDENTIALS") or "data/gdrive_oauth_credentials.json").strip()
    token_path = (os.environ.get("GDRIVE_TOKEN_PATH") or "data/gdrive_token.json").strip()

    oauth_file = (ROOT / oauth_path).resolve() if not Path(oauth_path).is_absolute() else Path(oauth_path)
    token_file = (ROOT / token_path).resolve() if not Path(token_path).is_absolute() else Path(token_path)
    token_file.parent.mkdir(parents=True, exist_ok=True)

    if not oauth_file.is_file():
        print(f"ERROR: No existe {oauth_file}")
        return 1

    scopes = ["https://www.googleapis.com/auth/drive.file"]
    flow = InstalledAppFlow.from_client_secrets_file(str(oauth_file), scopes=scopes)

    # port=0 → puerto aleatorio (más confiable en Windows)
    creds = flow.run_local_server(port=0)

    token_file.write_text(creds.to_json(), encoding="utf-8")
    print("Autorización exitosa")
    print(f"Token guardado en: {token_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

