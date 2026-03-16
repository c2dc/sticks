import json
import requests
import argparse
from pathlib import Path

CALDERA_URL = "http://127.0.0.1:8888"
API_KEY = "ADMIN123"   # change if you use a different key

def restore_abilities(abilities_file):
    abl_path = Path(abilities_file)
    if not abl_path.exists():
        print(f"❌ File not found: {abilities_file}")
        return False

    with open(abl_path, "r", encoding="utf-8") as f:
        try:
            abilities = json.load(f)
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse JSON: {e}")
            return False

    # Some exports may be a single object instead of a list
    if not isinstance(abilities, list):
        abilities = [abilities]

    headers = {
        "Content-Type": "application/json",
        "key": API_KEY
    }

    success_count = 0
    for abl in abilities:
        try:
            resp = requests.post(
                f"{CALDERA_URL}/api/v2/abilities",
                headers=headers,
                json=abl
            )
            if resp.status_code == 200:
                print(f"✅ Restored ability: {abl.get('name')} ({abl.get('ability_id')})")
                success_count += 1
            else:
                print(f"⚠️ Failed to restore {abl.get('name')}: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"❌ Error restoring {abl.get('name')}: {e}")

    print(f"\n📊 Restoration complete: {success_count}/{len(abilities)} abilities restored successfully")
    return success_count > 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restore CALDERA abilities from a JSON backup file")
    parser.add_argument("file", help="Path to the abilities JSON backup file")
    parser.add_argument("--url", default=CALDERA_URL, help=f"CALDERA server URL (default: {CALDERA_URL})")
    parser.add_argument("--key", default=API_KEY, help=f"API key (default: {API_KEY})")
    
    args = parser.parse_args()
    
    # Update global variables with command-line arguments
    CALDERA_URL = args.url.rstrip('/')  # Remove trailing slash if present
    API_KEY = args.key
    
    restore_abilities(args.file)
