# tools.py
from pathlib import Path
import requests
import os

try:
    import config
except ImportError as e:
    print(f"❌ Could not import from 'config': {e}")
    sys.exit(1)

HEADERS = {}
if getattr(config, "GITHUB_TOKEN", None):
    HEADERS["Authorization"] = f"token {config.GITHUB_TOKEN}"


def download_github_folder(owner, repo, folder_path, local_dir, branch="main"):
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{folder_path}?ref={branch}"
    resp = requests.get(api_url, headers=HEADERS)
    if resp.status_code != 200:
        raise Exception(f"Failed to fetch {folder_path}: {resp.status_code} {resp.text}")

    local_dir = Path(local_dir)
    os.makedirs(local_dir, exist_ok=True)

    for item in resp.json():
        if item["type"] == "file":
            file_resp = requests.get(item["download_url"], headers=HEADERS)
            file_resp.raise_for_status()
            with open(local_dir / item["name"], "wb") as f:
                f.write(file_resp.content)
            print(f"⬇️ {item['path']}")
        elif item["type"] == "dir":
            download_github_folder(owner, repo, f"{folder_path}/{item['name']}", local_dir / item["name"], branch)