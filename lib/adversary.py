# adversary.py
import json
import requests
import sys
from pathlib import Path
import yaml 
from stix2 import parse
from typing import List, Dict, Any


project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    import config
except ImportError:
    print("❌ Could not import 'config' module. Make sure your PYTHONPATH includes the project root.")
    sys.exit(1)

CALDERA_URL = config.CALDERA_URL.rstrip("/")
API_KEY = getattr(config, "CALDERA_API_KEY_RED", None)
CALDERA_ADVERSARIES_DIR = getattr(config, "CALDERA_ADVERSARIES_DIR", None)
HEADERS = {
    "KEY": API_KEY,
    "Content-Type": "application/json"
}

def load_stix_objects(file_path: Path) -> List[Any]:
    """Parse STIX objects from file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        objects = data.get("objects", [])
    except Exception as e:
        print(f"❌ Failed to load {file_path.name}: {e}")
        return []

    parsed = []
    for i, obj in enumerate(objects):
        try:
            parsed_obj = parse(obj, allow_custom=True)
            parsed.append(parsed_obj)
        except Exception as e:
            print(f"⚠️ Skipping object {i} in {file_path.name}: {e}")
    return parsed

def extract_adversary(apt_name: str, objects: List[Any]) -> Dict:
    """
    Generate YAML-like dict with abilities and executors for a given APT.
    """
    abilities = {}
    atomic_ordering = []

    for obj in objects:
        if getattr(obj, "type", "") == "attack-pattern":
            technique_id = getattr(obj, "external_references", [{}])[0].get("external_id", "no-id")
            technique_name = getattr(obj, "name", "no-name")
            description = getattr(obj, "description", "No description provided")
            tactic = getattr(obj, "x_mitre_tactic", "discovery")
            ability_id = str(getattr(obj, "id", technique_id))
            atomic_ordering.append(ability_id)

            executors = []

            # Example commands — can later be replaced by atomic commands
            executors.append({"sh": {"platform": "darwin", "command": "echo simulated command"}})
            executors.append({"sh": {"platform": "linux", "command": "echo simulated command"}})
            executors.append({"psh": {"platform": "windows", "command": "echo simulated command"}})

            abilities[ability_id] = {
                "name": technique_name,
                "description": description,  # <-- added
                "tactic": tactic,
                "technique_name": technique_name,
                "technique_id": technique_id,
                "executors": executors
            }

    return {
    "id": apt_name.lower().replace(" ", "_"),
    "name": apt_name,
    "description": "\n".join(
        getattr(obj, "description", "No description") 
        for obj in objects if getattr(obj, "type", "") == "intrusion-set"
    ),
    "objective": "your-objective-uuid",
    "atomic_ordering": atomic_ordering,
    "abilities": abilities
}

def save_adversary(output_path: Path, data: Dict):
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, sort_keys=False)
    print(f"✅ Saved: {output_path.name}")


def generate_adversaries():
    if not config.APT_DIR.exists():
        print(f"❌ APT_DIR does not exist: {config.APT_DIR}")
        return
    if not CALDERA_ADVERSARIES_DIR.exists():
        CALDERA_ADVERSARIES_DIR.mkdir(parents=True)
        print(f"📁 Created output directory: {CALDERA_ADVERSARIES_DIR}")

    for apt_file in config.APT_DIR.glob("*.json"):
        print(f"🔍 Processing {apt_file.name}...")
        objects = load_stix_objects(apt_file)
        intrusion_sets = [o for o in objects if getattr(o, "type", "") == "intrusion-set"]

        if not intrusion_sets:
            print(f"⚠️ No intrusion-set found in {apt_file.name}. Skipping.")
            continue

        apt = intrusion_sets[0]
        apt_name = getattr(apt, "name", "unknown")

        caldera_yaml = extract_adversary(apt_name, objects)
        output_file = CALDERA_ADVERSARIES_DIR / f"{apt_name.lower().replace(' ', '_')}.yml"
        save_adversary(output_file, caldera_yaml)

    print("\n🎉 CALDERA YAML generation completed.")



def upload_adversary(adversary_file: Path):
    """
    Uploads a single adversary YAML file to the Caldera server.
    Returns True if successful, False otherwise.
    """
    if not adversary_file.exists():
        print(f"❌ Adversary file not found: {adversary_file}")
        return False

    try:
        with open(adversary_file, "r", encoding="utf-8") as f:
            adversary_data = yaml.safe_load(f)

        url = f"{CALDERA_URL}/api/v2/adversaries"
        response = requests.post(url, headers=HEADERS, json=adversary_data)

        if response.status_code in (200, 201):
            print(f"✅ Uploaded adversary: {adversary_file.name}")
            return True
        else:
            print(f"❌ Failed to upload {adversary_file.name}: {response.status_code} - {response.text}")
            return False

    except yaml.YAMLError as e:
        print(f"❌ Invalid YAML in {adversary_file}: {e}")
        return False
    except Exception as e:
        print(f"❌ Error uploading {adversary_file}: {e}")
        return False


def upload_all_adversaries():
    """
    Uploads all adversary YAML files in CALDERA_ADVERSARIES_DIR.
    """
    if not CALDERA_ADVERSARIES_DIR.exists():
        print(f"❌ APT directory not found: {CALDERA_ADVERSARIES_DIR}")
        return

    files = list(CALDERA_ADVERSARIES_DIR.glob("*.yaml")) + list(CALDERA_ADVERSARIES_DIR.glob("*.yml"))
    if not files:
        print(f"⚠ No adversary YAML files found in {CALDERA_ADVERSARIES_DIR}")
        return

    success_count = 0
    for adversary_file in files:
        if upload_adversary(adversary_file):
            success_count += 1

    print(f"\n✅ Successfully uploaded {success_count} out of {len(files)} adversaries.")



def list_adversaries():
    """
    Lists all adversaries stored in the Caldera server.
    """
    try:
        url = f"{CALDERA_URL}/api/v2/adversaries"
        response = requests.get(url, headers=HEADERS)

        if response.status_code != 200:
            print(f"❌ Failed to list adversaries: {response.status_code} - {response.text}")
            return

        adversaries = response.json()
        if not adversaries:
            print("⚠ No adversaries found on the server.")
            return

        print(f"📋 Found {len(adversaries)} adversaries:")
        for adv in adversaries:
            adv_id = adv.get("adversary_id", "unknown")
            name = adv.get("name", "Unnamed")
            description = adv.get("description", "")
            print(f"- {name} (ID: {adv_id}) - {description}")
        print(f"📋 Listed {len(adversaries)} adversaries!")

    except Exception as e:
        print(f"❌ Error listing adversaries: {e}")


def show_help():
    """
    Prints available options for this script.
    """
    print("""
📌 Usage: python adversary.py <command> [args]

Commands:
  generate           Generate adversaries from STIX files in APT_DIR
  upload <file>      Upload a specific adversary YAML file
  upload_all         Upload all adversaries from CALDERA_ADVERSARIES_DIR
  list               List all adversaries from the Caldera server
  help               Show this help message

Examples:
  python adversary.py generate
  python adversary.py upload apt29.yml
  python adversary.py upload_all
  python adversary.py list
  python adversary.py help
""")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_help()
        sys.exit(1)
    
    cmd = sys.argv[1].lower()

    if cmd == "generate":
        print("🔄 Generating adversaries from STIX files...")
        generate_adversaries()
    
    elif cmd == "upload":
        if len(sys.argv) < 3:
            print("❌ Please specify a file to upload")
            print("Usage: python adversary.py upload <filename>")
            sys.exit(1)
        file_path = Path(sys.argv[2])
        upload_adversary(file_path)
    
    elif cmd == "upload_all":
        print("🔄 Uploading all adversaries...")
        upload_all_adversaries()
    
    elif cmd == "list":
        print("📋 Listing adversaries from CALDERA server...")
        list_adversaries()
    
    elif cmd in ["help", "-h", "--help"]:
        show_help()
    
    else:
        print(f"❌ Unknown command: {cmd}")
        show_help()
        sys.exit(1)