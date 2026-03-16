#!/usr/bin/env python3
"""
CALDERA Ability Manager - Final Version
Generates and uploads CALDERA abilities from STIX data in standardized JSON format
"""

import sys
import json
import uuid
import requests
from pathlib import Path
from typing import List, Dict, Any, Optional
from stix2 import parse
import yaml
import hashlib
from collections import defaultdict
import re

# Setup import path for config
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    import config
except ImportError as e:
    print(f"❌ Could not import from 'config': {e}")
    sys.exit(1)

try:
    import tools
except ImportError as e:
    print(f"❌ Could not import from 'tools': {e}")
    sys.exit(1)

# Configuration
CALDERA_URL = getattr(config, "CALDERA_URL", "http://localhost:8888").rstrip("/")
API_KEY = getattr(config, "CALDERA_API_KEY_RED", None)
HEADERS = {
    "KEY": API_KEY,
    "Content-Type": "application/json"
}
REQUEST_TIMEOUT = 30  # seconds

def show_help():
    """Display usage instructions"""
    print("""
📌 CALDERA Ability Manager - Usage Guide

Commands:
  generate          Generate ability JSON files from STIX data in APT_DIR
  generate_matrix   Generate abilities from enterprise-attack.json (full matrix)
  upload <file>     Upload a single ability JSON file to CALDERA
  upload_all        Upload all JSON abilities from CALDERA_ABILITIES_DIR
  get_atomic        Get atomic from github to ATOMIC_RED_DIR

Examples:
  python abilities.py generate
  python abilities.py generate_matrix
  python abilities.py upload apt29_t1059.json
  python abilities.py upload_all
  python abilities.py get_atomic

Configuration:
  Set these in config.py:
  - CALDERA_URL: Caldera server address (default: http://localhost:8888)
  - CALDERA_API_KEY_RED: API key for authentication
  - APT_DIR: Directory containing STIX JSON files
  - CALDERA_ABILITIES_DIR: Output directory for generated abilities
  - ATOMIC_RED_DIR: Output directory for atomic tecniques
""")


def get_atomic_commands(technique_id, executor_type=None, platform=None):
    """
    Get commands from Atomic Red Team YAML for a given technique ID.
    - executor_type: filter by executor ('sh', 'bash', 'psh', 'cmd')
    - platform: filter by platform ('windows', 'linux', 'macos')
    Returns all matching commands separated by two newlines.
    """
    technique_dir = Path(config.ATOMIC_RED_DIR) / technique_id
    yaml_file = technique_dir / f"{technique_id}.yaml"

    if not yaml_file.exists():
        raise FileNotFoundError(f"[DEBUG] Atomic YAML not found for {technique_id} at {yaml_file}")

    with open(yaml_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    commands = []
    tests = data.get("atomic_tests", [])
    if not tests:
        raise ValueError(f"[DEBUG] No atomic_tests found in YAML for {technique_id}")

    for test in tests:
        supported_platforms = test.get("supported_platforms", [])
        executor = test.get("executor", {})
        exec_name = executor.get("name")

        # Skip if executor or platform does not match
        if executor_type and exec_name != executor_type:
            continue
        if platform and platform not in supported_platforms:
            continue

        # Collect command(s)
        cmd = executor.get("command")
        if cmd:
            if isinstance(cmd, list):
                cmd = "\n".join(cmd)
            commands.append(cmd.strip())

        # Also check for any platform-specific commands inside 'platforms'
        platforms = executor.get("platforms", {})
        for plat, plat_data in platforms.items():
            plat_cmd = plat_data.get("command")
            if plat_cmd:
                if isinstance(plat_cmd, list):
                    plat_cmd = "\n".join(plat_cmd)
                # Only add if the platform matches
                if not platform or plat.lower() == platform.lower():
                    commands.append(plat_cmd.strip())

    if not commands:
        raise ValueError(f"[DEBUG] No commands found for {technique_id} "
                         f"(executor={executor_type}, platform={platform})")

    return "\n\n".join(commands)

def translate_all_caldera_abilities():
    abilities_dir = Path(config.CALDERA_ABILITIES_DIR)
    if not abilities_dir.exists() or not abilities_dir.is_dir():
        raise FileNotFoundError(f"CALDERA_ABILITIES_DIR not found: {abilities_dir}")

    stix_files = list(abilities_dir.glob("*.json"))
    if not stix_files:
        print(f"No JSON files found in {abilities_dir}")
        return

    for stix_file in stix_files:
        changed = False
        try:
            with open(stix_file, "r", encoding="utf-8") as f:
                stix_data = json.load(f)

            for executor in stix_data.get("executors", []):
                exec_type = executor.get("executor")  # e.g., 'sh', 'bash', 'psh', 'cmd'
                platform = executor.get("platform")   # e.g., 'linux', 'darwin', 'windows'
                cmd = executor.get("command", "")
                tids = re.findall(r"T\d{4}(?:\.\d+)?", cmd)

                if not tids:
                    continue

                for tid in tids:
                    try:
                        # get_atomic_commands filters by executor type AND platform
                        new_cmds = get_atomic_commands(tid, executor_type=exec_type, platform=platform)
                        if new_cmds and new_cmds != cmd:
                            executor["command"] = new_cmds
                            changed = True
                            print(f"   🔄 Replaced {tid} ({exec_type}/{platform}) in {stix_file.name}")
                    except Exception as e:
                        print(f"   ⚠️ Skipped {tid} ({exec_type}/{platform}) in {stix_file.name}: {e}")

            if changed:
                with open(stix_file, "w", encoding="utf-8") as f:
                    json.dump(stix_data, f, indent=2)
                print(f"✅ Updated {stix_file.name}")
            else:
                print(f"⚠️ No changes made to {stix_file.name}")

        except Exception as e:
            print(f"❌ Failed to process {stix_file.name}: {e}")

##### do not touch above

def load_stix_objects(file_path: Path) -> List[Any]:
    """Load and parse STIX objects from JSON file with fallback to raw dicts"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        objects = data.get("objects", [])
        parsed_objects = []
        
        for obj in objects:
            try:
                parsed_obj = parse(obj, allow_custom=True)
                parsed_objects.append(parsed_obj)
            except Exception:
                # If parsing fails, use raw dict for attack-patterns
                if obj.get("type") == "attack-pattern":
                    parsed_objects.append(obj)
                # Silently skip other types
                
        return parsed_objects
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in {file_path.name}: {e}")
    except Exception as e:
        print(f"❌ Failed to load {file_path.name}: {e}")
    return []

def generate_ability(technique: Any, apt_name: str = "unknown") -> Optional[Dict[str, Any]]:
    """Generate CALDERA ability with guaranteed executors - includes PRE techniques"""
    try:
        # Handle both parsed STIX objects and raw dicts
        if isinstance(technique, dict):
            tech_dict = technique
        else:
            tech_dict = None
        
        # 1. Extract core fields with validation
        tech_id_attr = getattr(technique, 'id', None) if not isinstance(technique, dict) else technique.get('id')
        if not tech_id_attr:
            tech_name = getattr(technique, 'name', None) if not isinstance(technique, dict) else technique.get('name', 'unnamed')
            print(f"⚠️ Skipping technique without ID: {tech_name}")
            return None

        # Extract external references
        if isinstance(technique, dict):
            external_refs = technique.get("external_references", [])
        else:
            external_refs = getattr(technique, "external_references", [])
        
        tech_id = "TXXXX"
        for ref in external_refs:
            if isinstance(ref, dict):
                if ref.get("source_name") == "mitre-attack":
                    tech_id = ref.get("external_id", "TXXXX")
                    break
            elif hasattr(ref, 'source_name') and ref.source_name == 'mitre-attack':
                if hasattr(ref, 'external_id'):
                    tech_id = ref.external_id
                    break

        # 2. Check if this is a PRE technique
        if isinstance(technique, dict):
            x_mitre_platforms = technique.get('x_mitre_platforms', [])
        else:
            x_mitre_platforms = getattr(technique, 'x_mitre_platforms', [])
        
        is_pre_technique = False
        if x_mitre_platforms:
            platforms_upper = [str(p).upper().strip() for p in x_mitre_platforms]
            is_pre_technique = any(platform in ['PRE', 'PRE-ATTACK', 'PRE-ATT&CK', 'PRE-COMPROMISE'] 
                                  for platform in platforms_upper)

        # 3. Create default executors
        executors = []
        platforms = ["linux", "windows", "darwin"]  # Supported platforms
        
        # Get technique name
        if isinstance(technique, dict):
            tech_name = technique.get('name', 'Unnamed')
        else:
            tech_name = getattr(technique, 'name', 'Unnamed')
        
        for platform in platforms:
            executor_name = "cmd" if platform == "windows" else "sh"
            
            # Different command based on technique type
            if is_pre_technique:
                command = f"echo 'NOCOMMAND: PRE-ATT&CK technique {tech_id} - {tech_name}'"
            else:
                command = f"echo 'Default command for {tech_id}'"
            
            executors.append({
                "name": executor_name,
                "platform": platform,
                "command": command,
                "cleanup": [],
                "payloads": [],
                "timeout": 60,
                "parsers": []
            })

        # 4. Get description
        if isinstance(technique, dict):
            description = technique.get('description', '')
        else:
            description = getattr(technique, 'description', '')

        # 5. Build ability with mandatory fields
        return {
            "ability_id": tech_id_attr,
            "tactic": "execution",  # Default tactic
            "technique_id": tech_id,
            "technique_name": tech_name,
            "name": f"{apt_name} - {tech_name}",
            "description": description,
            "executors": executors,  # Guaranteed to exist
            "requirements": [],
            "privilege": "",
            "repeatable": False,
            "plugin": "atomic"
        }

    except Exception as e:
        print(f"❌ Failed to generate ability: {str(e)}")
        import traceback
        traceback.print_exc()
        return None
    

def save_json(path: Path, data: dict) -> bool:
    """Save ability to JSON file with error handling"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"✅ Ability saved: {path.name}")
        return True
    except Exception as e:
        print(f"❌ Failed to save {path.name}: {e}")
        return False

def generate_abilities() -> int:
    """Generate unique abilities (1 per technique ID) from STIX files in APT_DIR"""
    output_dir = config.CALDERA_ABILITIES_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    apt_files = list(config.APT_DIR.glob("*.json"))

    if not apt_files:
        print(f"⚠️ No APT JSON files found in {config.APT_DIR}")
        return 0

    # technique_id → merged ability
    abilities_by_tid = {}

    for apt_file in apt_files:
        objects = load_stix_objects(apt_file)
        if not objects:
            continue

        intrusion_sets = [o for o in objects if o.type == "intrusion-set"]
        techniques = [o for o in objects if o.type == "attack-pattern"]

        if not intrusion_sets:
            print(f"⚠️ No intrusion-set in {apt_file.name}, skipping.")
            continue

        apt_name = intrusion_sets[0].name

        for tech in techniques:
            ability = generate_ability(tech, apt_name)
            if not ability:
                continue

            tid = ability["technique_id"]

            if tid not in abilities_by_tid:
                # first time seeing this technique → create entry
                abilities_by_tid[tid] = ability
                abilities_by_tid[tid].setdefault("tags", []).append(apt_name)
            else:
                # already exists → just merge tags
                abilities_by_tid[tid].setdefault("tags", []).append(apt_name)

    # Save all unique abilities
    for tid, ability in abilities_by_tid.items():
        filename = f"{tid.lower()}.json"
        filepath = output_dir / filename

        if save_json(filepath, ability):
            count += 1

    print(f"\n🎯 Successfully generated {count} unique CALDERA abilities")

    return count

def generate_abilities_from_matrix() -> int:
    """Generate abilities from enterprise-attack.json (full ATT&CK matrix) - includes all techniques"""
    output_dir = config.CALDERA_ABILITIES_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # Try common locations for enterprise-attack.json
    possible_paths = [
        Path("data/stix/enterprise-attack.json"),
        Path("data/enterprise-attack.json"),
        Path("enterprise-attack.json"),
        config.APT_DIR / "enterprise-attack.json"
    ]
    
    matrix_file = None
    for path in possible_paths:
        if path.exists():
            matrix_file = path
            break
    
    if not matrix_file:
        print("❌ Could not find enterprise-attack.json")
        print("   Tried locations:")
        for p in possible_paths:
            print(f"   - {p}")
        print("\n   Please specify the path or move the file to one of these locations")
        return 0

    print(f"\n📄 Processing full ATT&CK matrix: {matrix_file}")
    
    objects = load_stix_objects(matrix_file)
    if not objects:
        print(f"  ⚠️ No objects loaded from {matrix_file.name}")
        return 0
    
    # Find all attack-patterns (techniques) - handle both parsed objects and dicts
    techniques = []
    for obj in objects:
        if isinstance(obj, dict):
            if obj.get("type") == "attack-pattern":
                techniques.append(obj)
        else:
            if getattr(obj, 'type', None) == "attack-pattern":
                techniques.append(obj)
    
    print(f"  📊 Found {len(techniques)} attack patterns in matrix")
    
    if not techniques:
        print(f"  ⚠️ No attack patterns found in {matrix_file.name}")
        return 0
    
    # Track statistics
    stats = {
        "total": len(techniques),
        "pre_techniques": 0,
        "regular_techniques": 0,
        "saved": 0
    }
    
    # Generate abilities for all techniques
    for tech in techniques:
        ability = generate_ability(tech, "ATT&CK Matrix")
        if not ability:
            continue
        
        # Check if PRE technique for stats
        if isinstance(tech, dict):
            x_mitre_platforms = tech.get('x_mitre_platforms', [])
        else:
            x_mitre_platforms = getattr(tech, 'x_mitre_platforms', [])
        
        if x_mitre_platforms:
            platforms_upper = [str(p).upper().strip() for p in x_mitre_platforms]
            is_pre = any(platform in ['PRE', 'PRE-ATTACK', 'PRE-ATT&CK', 'PRE-COMPROMISE'] 
                        for platform in platforms_upper)
            if is_pre:
                stats["pre_techniques"] += 1
            else:
                stats["regular_techniques"] += 1
        else:
            stats["regular_techniques"] += 1
        
        # Save the ability
        tid = ability["technique_id"]
        filename = f"{tid.lower()}.json"
        filepath = output_dir / filename
        
        if save_json(filepath, ability):
            stats["saved"] += 1

    # Print statistics
    print(f"\n{'='*60}")
    print(f"📊 MATRIX GENERATION STATISTICS")
    print(f"{'='*60}")
    print(f"Total techniques found: {stats['total']}")
    print(f"  - PRE techniques: {stats['pre_techniques']}")
    print(f"  - Regular techniques: {stats['regular_techniques']}")
    print(f"Abilities saved: {stats['saved']}")
    print(f"{'='*60}")

    return stats["saved"]

def upload_all_abilities():
    """Upload all JSON ability files with basic progress counter"""
    abilities_dir = config.CALDERA_ABILITIES_DIR
    if not abilities_dir.exists():
        print(f"❌ Abilities directory not found: {abilities_dir}")
        return

    json_files = list(abilities_dir.glob("*.json"))
    if not json_files:
        print(f"⚠️ No JSON ability files found in {abilities_dir}")
        return

    total = len(json_files)
    success = 0

    for i, json_file in enumerate(json_files, 1):
        print(f"[{i}/{total}] Trying to upload {json_file.name}...", end=" ")
        if upload_ability(json_file):
            success += 1
            print(f"✅ [{i}/{total}] Uploaded {json_file.name}...")
        else:
            print(f"❌ [{i}/{total}] Not uploaded {json_file.name}...")

    print(f"\nUpload complete: {success}/{total} succeeded")

def upload_ability(json_file: Path) -> bool:
    """Robust upload function with detailed error reporting"""
    try:
        # 1. File Validation
        if not json_file.exists():
            print(f"🛑 File not found: {json_file.name}")
            return False

        # 2. JSON Validation
        try:
            with open(json_file, "r") as f:
                ability_data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"🛑 Invalid JSON in {json_file.name}: {str(e)}")
            return False

        # 3. Field Validation
        required_fields = ["ability_id", "tactic", "technique_id", "executors"]
        for field in required_fields:
            if field not in ability_data:
                print(f"🛑 Missing required field '{field}' in {json_file.name}")
                return False

        # 4. Size Validation (CALDERA typically limits to 1MB)
        if len(json.dumps(ability_data)) > 1_000_000:
            print(f"🛑 File too large (>1MB): {json_file.name}")
            return False

        # 5. API Request with Debugging
        response = requests.post(
            f"{CALDERA_URL}/api/v2/abilities",
            headers=HEADERS,
            json=ability_data,
            timeout=30
        )

        # 6. Response Handling
        if response.status_code == 200:
            return True
        if response.status_code == 400:
            print(f"⚠️ {json_file.name} already exists!")
            return True
            
        error_msg = response.json().get("error", "No error details")
        print(f"🛑 Failed {json_file.name} (HTTP {response.status_code}): {error_msg}")
        
        # Special handling for common errors
        if "already exists" in error_msg.lower():
            print("💡 Tip: Ability ID collision - try regenerating with fresh IDs")
        elif "invalid tactic" in error_msg.lower():
            print("💡 Tip: Validate tactic naming conventions")
            
        return False
    except KeyboardInterrupt:
        print(f"\n****************************")
        print(f"🌐 At your command!")
        print(f"****************************")
        return exit(0)
    except requests.exceptions.RequestException as e:
        print(f"🌐 Network error on {json_file.name}: {str(e)}")
        return False
    except Exception as e:
        print(f"⚠️ Unexpected error with {json_file.name}: {str(e)}")
        return False

def get_atomic(branch="master"):
    """
    Download the 'atomics' folder from the atomic-red-team GitHub repo
    into the directory specified by config.ATOMIC_RED_DIR.
    """
    tools.download_github_folder(
        owner="redcanaryco",
        repo="atomic-red-team",
        folder_path="atomics",
        local_dir=config.ATOMIC_RED_DIR,
        branch=branch
    )
    print("✅ Atomics download complete.")


def main():
    if len(sys.argv) < 2:
        show_help()
        sys.exit(0)

    command = sys.argv[1].lower()

    try:
        if command == "generate":
            generate_abilities()
            translate_all_caldera_abilities()
        elif command == "generate_matrix":
            generate_abilities_from_matrix()
            translate_all_caldera_abilities()
        elif command == "upload":
            if len(sys.argv) != 3:
                print("❌ Please provide the JSON ability file to upload")
                sys.exit(1)
            upload_ability(Path(sys.argv[2]))
        elif command == "upload_all":
            upload_all_abilities()
        elif command == "get_atomic":
            get_atomic()
        else:
            show_help()
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n🛑 Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
