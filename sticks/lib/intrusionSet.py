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
    """Parse STIX objects from file with fallback to raw dicts."""
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
            # If parsing fails, use raw dict for attack-patterns, campaigns, and intrusion-sets
            if obj.get("type") in ["attack-pattern", "campaign", "intrusion-set"]:
                print(f"⚠️ STIX parse failed for {obj.get('type')} #{i}, using raw dict")
                parsed.append(obj)
            else:
                print(f"⚠️ Skipping object {i} in {file_path.name}: {e}")
    return parsed

def get_object_attribute(obj: Any, attr_name: str, default: Any = None) -> Any:
    """Get attribute from either parsed STIX object or raw dict."""
    if isinstance(obj, dict):
        return obj.get(attr_name, default)
    else:
        return getattr(obj, attr_name, default)

def extract_adversary(adversary_name: str, adversary_id: str, adversary_ext_id: str, objects: List[Any]) -> Dict:
    """
    Generate YAML-like dict with abilities and executors for a given intrusion-set.
    
    Args:
        adversary_name: Name of the adversary
        adversary_id: STIX ID of the intrusion-set
        adversary_ext_id: MITRE external ID (e.g., G0096)
        objects: List of STIX objects
    """
    abilities = {}
    atomic_ordering = []
    
    # Find all relationships where this intrusion-set is the source
    related_technique_ids = set()
    
    for obj in objects:
        obj_type = get_object_attribute(obj, "type", "")
        
        if obj_type == "relationship":
            source_ref = get_object_attribute(obj, "source_ref", "")
            target_ref = get_object_attribute(obj, "target_ref", "")
            relationship_type = get_object_attribute(obj, "relationship_type", "")
            
            # Check if this relationship links our intrusion-set to an attack-pattern
            # Match by STIX ID or by checking if source contains our external ID
            source_matches = (
                source_ref == adversary_id or 
                (adversary_ext_id and adversary_ext_id.lower() in source_ref.lower())
            )
            
            if source_matches and relationship_type == "uses" and target_ref.startswith("attack-pattern--"):
                related_technique_ids.add(target_ref)

    # If no relationships found by STIX ID, try to find techniques by external ID references
    if not related_technique_ids and adversary_ext_id:
        print(f"    🔍 No direct STIX relationships found, searching by external ID: {adversary_ext_id}")

    # Build a mapping of attack-pattern STIX IDs to external IDs
    technique_id_map = {}
    for obj in objects:
        obj_type = get_object_attribute(obj, "type", "")
        if obj_type == "attack-pattern":
            obj_id = get_object_attribute(obj, "id", "")
            external_refs = get_object_attribute(obj, "external_references", [])
            
            for ref in external_refs:
                if isinstance(ref, dict):
                    if ref.get("source_name") == "mitre-attack":
                        ext_id = ref.get("external_id")
                        if ext_id:
                            technique_id_map[obj_id] = ext_id
                        break
                elif hasattr(ref, 'source_name') and ref.source_name == 'mitre-attack':
                    ext_id = getattr(ref, 'external_id', None)
                    if ext_id:
                        technique_id_map[obj_id] = ext_id
                    break

    # If no relationships found, process all attack-patterns (legacy behavior)
    if not related_technique_ids:
        print(f"    ℹ️ No relationships found, processing all attack-patterns in file")
        process_all = True
    else:
        print(f"    ℹ️ Found {len(related_technique_ids)} related techniques via relationships")
        process_all = False

    for obj in objects:
        obj_type = get_object_attribute(obj, "type", "")
        
        if obj_type == "attack-pattern":
            obj_id = get_object_attribute(obj, "id", "")
            
            # Skip if we have relationships and this technique isn't related
            if not process_all and obj_id not in related_technique_ids:
                continue
            
            # Get external references
            external_refs = get_object_attribute(obj, "external_references", [])
            
            # Extract technique ID
            technique_id = "no-id"
            for ref in external_refs:
                if isinstance(ref, dict):
                    if ref.get("source_name") == "mitre-attack":
                        technique_id = ref.get("external_id", "no-id")
                        break
                elif hasattr(ref, 'source_name') and ref.source_name == 'mitre-attack':
                    technique_id = getattr(ref, 'external_id', "no-id")
                    break
            
            technique_name = get_object_attribute(obj, "name", "no-name")
            description = get_object_attribute(obj, "description", "No description provided")
            
            # Get tactic from kill_chain_phases
            kill_chain_phases = get_object_attribute(obj, "kill_chain_phases", [])
            tactic = "discovery"  # default
            if kill_chain_phases:
                phase = kill_chain_phases[0]
                if isinstance(phase, dict):
                    tactic = phase.get("phase_name", "discovery")
                elif hasattr(phase, 'phase_name'):
                    tactic = phase.phase_name
            
            ability_id = obj_id if obj_id else technique_id
            if not isinstance(ability_id, str):
                ability_id = str(ability_id)
            
            atomic_ordering.append(ability_id)

            executors = []

            # Example commands — can later be replaced by atomic commands
            executors.append({"sh": {"platform": "darwin", "command": f"echo 'NOCOMMAND: No atomic command for {technique_id}'"}})
            executors.append({"sh": {"platform": "linux", "command": f"echo 'NOCOMMAND: No atomic command for {technique_id}'"}})
            executors.append({"psh": {"platform": "windows", "command": f"echo 'NOCOMMAND: No atomic command for {technique_id}'"}})

            abilities[ability_id] = {
                "name": technique_name,
                "description": description,
                "tactic": tactic,
                "technique_name": technique_name,
                "technique_id": technique_id,
                "executors": executors
            }

    # Build description from intrusion-set object
    description = ""
    for obj in objects:
        obj_type = get_object_attribute(obj, "type", "")
        if obj_type == "intrusion-set":
            obj_id = get_object_attribute(obj, "id", "")
            if obj_id == adversary_id:
                desc = get_object_attribute(obj, "description", "")
                if desc and desc != "No description":
                    description = desc
                    break
    
    if not description:
        description = f"Intrusion Set: {adversary_name}"

    # Add "1." prefix to adversary name for the output
    prefixed_adversary_name = f"1.{adversary_name}"

    return {
        "id": prefixed_adversary_name.lower().replace(" ", "_").replace(".", "_"),
        "name": prefixed_adversary_name,  # Add "1." prefix to name
        "description": description,
        "objective": "your-objective-uuid",
        "atomic_ordering": atomic_ordering,
        "abilities": abilities
    }

def save_adversary(output_path: Path, data: Dict):
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, sort_keys=False)
    print(f"✅ Saved: {output_path.name}")


def list_intrusion_sets():
    """List all intrusion-sets found in APT_DIR files."""
    if not config.APT_DIR.exists():
        print(f"❌ APT_DIR does not exist: {config.APT_DIR}")
        return

    all_intrusion_sets = []
    
    for apt_file in config.APT_DIR.glob("*.json"):
        objects = load_stix_objects(apt_file)
        
        for obj in objects:
            obj_type = get_object_attribute(obj, "type", "")
            
            if obj_type == "intrusion-set":
                name = get_object_attribute(obj, "name", "unknown")
                obj_id = get_object_attribute(obj, "id", "")
                all_intrusion_sets.append({
                    "name": name,
                    "stix_id": obj_id,
                    "file": apt_file.name
                })
    
    print(f"\n{'='*60}")
    print(f"📋 INTRUSION-SETS FOUND ({len(all_intrusion_sets)})")
    print(f"{'='*60}")
    for i, apt in enumerate(all_intrusion_sets, 1):
        print(f"{i}. {apt['name']}")
        print(f"   File: {apt['file']}")
        print(f"   STIX ID: {apt['stix_id'][:40]}...")
    
    print(f"\n{'='*60}")
    print(f"Total: {len(all_intrusion_sets)} intrusion-sets")
    print(f"{'='*60}")


def generate_adversaries():
    """Generate adversaries only from intrusion-sets (ignore campaigns)."""
    if not config.APT_DIR.exists():
        print(f"❌ APT_DIR does not exist: {config.APT_DIR}")
        return
    if not CALDERA_ADVERSARIES_DIR.exists():
        CALDERA_ADVERSARIES_DIR.mkdir(parents=True)
        print(f"📁 Created output directory: {CALDERA_ADVERSARIES_DIR}")

    stats = {
        "total_files": 0,
        "intrusion_sets_found": 0,
        "intrusion_sets_generated": 0,
        "intrusion_sets_skipped_no_techniques": 0,
        "files_skipped_empty": 0
    }

    for apt_file in config.APT_DIR.glob("*.json"):
        stats["total_files"] += 1
        print(f"\n🔍 Processing {apt_file.name}...")
        objects = load_stix_objects(apt_file)
        
        if not objects:
            print(f"  ⚠️ No objects loaded from {apt_file.name}")
            stats["files_skipped_empty"] += 1
            continue
        
        # Only look for intrusion-sets
        intrusion_sets = []
        attack_patterns = []
        
        for obj in objects:
            obj_type = get_object_attribute(obj, "type", "")
            if obj_type == "intrusion-set":
                intrusion_sets.append(obj)
            elif obj_type == "attack-pattern":
                attack_patterns.append(obj)
        
        print(f"  📊 Found: {len(intrusion_sets)} intrusion-sets, {len(attack_patterns)} attack-patterns")
        
        stats["intrusion_sets_found"] += len(intrusion_sets)

        # Process intrusion-sets
        for intrusion_set in intrusion_sets:
            apt_name = get_object_attribute(intrusion_set, "name", "unknown")
            apt_id = get_object_attribute(intrusion_set, "id", "")
            
            # Extract external ID (e.g., G0096)
            external_refs = get_object_attribute(intrusion_set, "external_references", [])
            apt_ext_id = None
            for ref in external_refs:
                if isinstance(ref, dict):
                    if ref.get("source_name") == "mitre-attack":
                        apt_ext_id = ref.get("external_id")
                        break
                elif hasattr(ref, 'source_name') and ref.source_name == 'mitre-attack':
                    apt_ext_id = getattr(ref, 'external_id', None)
                    break
            
            print(f"  🎯 Processing intrusion-set: {apt_name} (ID: {apt_id[:20]}..., Ext: {apt_ext_id or 'N/A'})")
            
            caldera_yaml = extract_adversary(apt_name, apt_id, apt_ext_id, objects)
            
            # Skip if no abilities were found
            if not caldera_yaml.get("abilities"):
                print(f"    ⚠️ No techniques found for intrusion-set {apt_name}, skipping")
                stats["intrusion_sets_skipped_no_techniques"] += 1
                continue
            
            # Add "1." prefix to output file name
            output_file = CALDERA_ADVERSARIES_DIR / f"1.{apt_name.lower().replace(' ', '_')}.yml"
            save_adversary(output_file, caldera_yaml)
            stats["intrusion_sets_generated"] += 1

    # Print detailed statistics
    print(f"\n{'='*60}")
    print(f"📊 ADVERSARY GENERATION STATISTICS")
    print(f"{'='*60}")
    print(f"Total files processed: {stats['total_files']}")
    print(f"Files skipped (empty): {stats['files_skipped_empty']}")
    print(f"\n📋 Discovery:")
    print(f"  Intrusion-sets found: {stats['intrusion_sets_found']}")
    print(f"\n✅ Successfully Generated:")
    print(f"  Intrusion-sets: {stats['intrusion_sets_generated']}")
    print(f"\n⚠️  Skipped (no techniques):")
    print(f"  Intrusion-sets: {stats['intrusion_sets_skipped_no_techniques']}")
    print(f"\n📦 Total adversaries generated: {stats['intrusion_sets_generated']}")
    print(f"{'='*60}")
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
    Uploads all intrusion set adversary YAML files starting with "1." 
    in CALDERA_ADVERSARIES_DIR.
    """
    if not CALDERA_ADVERSARIES_DIR.exists():
        print(f"❌ Adversaries directory not found: {CALDERA_ADVERSARIES_DIR}")
        return

    # Get only files starting with "1."
    files = list(CALDERA_ADVERSARIES_DIR.glob("1.*.yaml")) + list(CALDERA_ADVERSARIES_DIR.glob("1.*.yml"))
    
    if not files:
        print(f"⚠ No adversary YAML files with '1.' prefix found in {CALDERA_ADVERSARIES_DIR}")
        print(f"  (Looking for files starting with '1.' like '1.apt29.yml')")
        return

    print(f"📁 Found {len(files)} adversary files with '1.' prefix:")
    for f in files:
        print(f"  - {f.name}")
    
    success_count = 0
    for adversary_file in files:
        print(f"\n🔄 Uploading adversary: {adversary_file.name}")
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
            description = adv.get("description", "")[:100]  # Truncate long descriptions
            print(f"- {name} (ID: {adv_id})")
            if description:
                print(f"  {description}...")
        print(f"\n📋 Listed {len(adversaries)} adversaries!")

    except Exception as e:
        print(f"❌ Error listing adversaries: {e}")


def show_help():
    """
    Prints available options for this script.
    """
    print("""
📌 Usage: python adversary.py <command> [args]

Commands:
  generate           Generate adversaries only from intrusion-sets (ignores campaigns)
  list_sources       List all intrusion-sets found in STIX files
  upload <file>      Upload a specific adversary YAML file
  upload_all         Upload all adversaries with '1.' prefix from CALDERA_ADVERSARIES_DIR
  list               List all adversaries from the Caldera server
  help               Show this help message

Examples:
  python adversary.py generate
  python adversary.py list_sources
  python adversary.py upload 1.apt29.yml
  python adversary.py upload_all
  python adversary.py list
  python adversary.py help

Note: 
  - This script only processes intrusion-sets, not campaigns.
  - Generated files have '1.' prefix (e.g., '1.apt29.yml')
  - Adversary names also have '1.' prefix in the YAML content
  - upload_all only uploads files starting with '1.'
""")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_help()
        sys.exit(1)
    
    cmd = sys.argv[1].lower()

    if cmd == "generate":
        print("🔄 Generating adversaries from STIX files (intrusion-sets only)...")
        generate_adversaries()
    
    elif cmd == "list_sources":
        print("🔍 Listing all intrusion-sets from STIX files...")
        list_intrusion_sets()
    
    elif cmd == "upload":
        if len(sys.argv) < 3:
            print("❌ Please specify a file to upload")
            print("Usage: python adversary.py upload <filename>")
            sys.exit(1)
        file_path = Path(sys.argv[2])
        upload_adversary(file_path)
    
    elif cmd == "upload_all":
        print("🔄 Uploading all adversaries with '1.' prefix...")
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
