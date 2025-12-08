# campaign.py
import json
import requests
import sys
from pathlib import Path
import yaml
from stix2 import parse
from typing import List, Dict, Any
import re


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

def extract_technique_id(attack_pattern_obj) -> str:
    """Extract MITRE ATT&CK technique ID from attack pattern object."""
    # Try to get external references from the STIX2 object
    if hasattr(attack_pattern_obj, 'external_references'):
        for ref in attack_pattern_obj.external_references:
            if hasattr(ref, 'source_name') and ref.source_name == 'mitre-attack':
                if hasattr(ref, 'external_id'):
                    return ref.external_id
    
    # Try to get from name attribute
    if hasattr(attack_pattern_obj, 'name'):
        name = attack_pattern_obj.name
        technique_match = re.search(r'T\d+\.?\d*', name)
        if technique_match:
            return technique_match.group()
    
    return None

def extract_technique_details(attack_pattern_obj) -> Dict:
    """Extract technique details from attack pattern object."""
    details = {
        "name": getattr(attack_pattern_obj, "name", "Unknown Technique"),
        "description": getattr(attack_pattern_obj, "description", "No description provided"),
        "tactic": "discovery"
    }
    
    # Extract tactic if available
    if hasattr(attack_pattern_obj, "kill_chain_phases"):
        kill_chain = getattr(attack_pattern_obj, "kill_chain_phases", [])
        if kill_chain:
            # Get the first phase name
            phase_name = kill_chain[0].get("phase_name", "discovery") if isinstance(kill_chain[0], dict) else kill_chain[0].phase_name
            details["tactic"] = phase_name
    
    return details

def extract_campaign_techniques(campaign_id: str, objects: List[Any]) -> List[Dict]:
    """Extract all techniques used by a specific campaign."""
    techniques = []
    
    # First, get all attack patterns
    attack_patterns = {}
    for obj in objects:
        if getattr(obj, "type", "") == "attack-pattern":
            attack_patterns[obj.id] = obj
    
    print(f"    🔍 Found {len(attack_patterns)} attack patterns")
    
    # Then, find all relationships where campaign uses attack patterns
    relationships_found = 0
    for obj in objects:
        if getattr(obj, "type", "") == "relationship":
            source_ref = getattr(obj, "source_ref", "")
            relationship_type = getattr(obj, "relationship_type", "")
            target_ref = getattr(obj, "target_ref", "")
            
            if source_ref == campaign_id and relationship_type == "uses" and target_ref in attack_patterns:
                relationships_found += 1
                attack_pattern = attack_patterns[target_ref]
                technique_id = extract_technique_id(attack_pattern)
                
                if technique_id:
                    technique_details = extract_technique_details(attack_pattern)
                    
                    techniques.append({
                        "id": attack_pattern.id,
                        "technique_id": technique_id,
                        "name": technique_details["name"],
                        "description": technique_details["description"],
                        "tactic": technique_details["tactic"]
                    })
                else:
                    print(f"    ⚠️ Could not extract technique ID from attack pattern")
    
    print(f"    🔗 Found {relationships_found} 'uses' relationships for this campaign")
    return techniques

def extract_campaign(campaign_name: str, objects: List[Any]) -> Dict:
    """
    Generate YAML-like dict with abilities and executors for a given campaign.
    """
    abilities = {}
    atomic_ordering = []
    
    # Find the campaign object
    campaign_obj = None
    for obj in objects:
        if getattr(obj, "type", "") == "campaign":
            if getattr(obj, "name", "").lower() == campaign_name.lower():
                campaign_obj = obj
                break
    
    if not campaign_obj:
        print(f"    ⚠️ Campaign '{campaign_name}' not found in objects")
        return {}
    
    campaign_id = getattr(campaign_obj, "id", "")
    print(f"    📝 Campaign ID: {campaign_id}")
    
    # Extract techniques used by this campaign
    techniques = extract_campaign_techniques(campaign_id, objects)
    
    if not techniques:
        print(f"    ⚠️ No techniques found for campaign '{campaign_name}'")
        return {}
    
    print(f"    🎯 Found {len(techniques)} techniques for campaign '{campaign_name}'")
    
    for technique in techniques:
        ability_id = technique["id"]
        technique_id = technique["technique_id"]
        technique_name = technique["name"]
        description = technique["description"]
        tactic = technique["tactic"]
        
        atomic_ordering.append(ability_id)
        
        # Create executors for different platforms
        executors = []
        
        # Linux executor
        executors.append({
            "sh": {
                "platform": "linux",
                "command": f"echo 'Simulated execution of {technique_id}: {technique_name}'"
            }
        })
        
        # Windows executor
        executors.append({
            "psh": {
                "platform": "windows",
                "command": f"Write-Host 'Simulated execution of {technique_id}: {technique_name}'"
            }
        })
        
        # macOS executor
        executors.append({
            "sh": {
                "platform": "darwin",
                "command": f"echo 'Simulated execution of {technique_id}: {technique_name}'"
            }
        })
        
        abilities[ability_id] = {
            "id": ability_id,
            "name": technique_name,
            "description": description,
            "tactic": tactic,
            "technique_name": technique_name,
            "technique_id": technique_id,
            "executors": executors
        }
    
    # Create campaign description
    campaign_description = getattr(campaign_obj, "description", "No description available")
    
    # Get aliases if available
    aliases = getattr(campaign_obj, "aliases", [])
    if aliases:
        campaign_description += f"\n\nAlso known as: {', '.join(aliases)}"
    
    return {
        "id": campaign_name.lower().replace(" ", "_").replace("-", "_"),
        "name": f"0.{campaign_name}",  # Add prefix "0." to name
        "description": campaign_description,
        "objective": "495a9828-cab1-44dd-a0ca-66e58177d8cc",  # Default objective ID
        "atomic_ordering": atomic_ordering,
        "abilities": abilities
    }

def save_campaign(output_path: Path, data: Dict):
    """Save campaign data as YAML file."""
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
        print(f"    ✅ Saved: {output_path.name}")
        return True
    except Exception as e:
        print(f"    ❌ Failed to save {output_path.name}: {e}")
        return False

def generate_campaigns():
    """Generate campaigns from STIX files."""
    # Check if STIX_FILE is a file or directory
    if not hasattr(config, 'STIX_FILE'):
        print(f"❌ STIX_FILE not configured in config.py")
        return
    
    stix_path = Path(config.STIX_FILE)
    
    if not stix_path.exists():
        print(f"❌ STIX path does not exist: {stix_path}")
        return
    
    if not CALDERA_ADVERSARIES_DIR:
        print(f"❌ CALDERA_ADVERSARIES_DIR not configured in config.py")
        return
    
    # Create output directory if it doesn't exist
    if not CALDERA_ADVERSARIES_DIR.exists():
        CALDERA_ADVERSARIES_DIR.mkdir(parents=True)
        print(f"📁 Created output directory: {CALDERA_ADVERSARIES_DIR}")
    
    # Get STIX files to process
    stix_files = []
    
    if stix_path.is_file():
        # Single STIX file
        print(f"📄 Processing single STIX file: {stix_path.name}")
        stix_files = [stix_path]
    elif stix_path.is_dir():
        # Directory containing STIX files
        print(f"📁 Processing STIX files in directory: {stix_path}")
        stix_extensions = ['.json', '.stix', '.stix2']
        for ext in stix_extensions:
            stix_files.extend(stix_path.glob(f"*{ext}"))
    else:
        print(f"❌ STIX_FILE is neither a file nor a directory: {stix_path}")
        return
    
    if not stix_files:
        print(f"⚠️ No STIX files found at {stix_path}")
        return
    
    print(f"🔍 Found {len(stix_files)} STIX file(s) to process")
    
    total_campaigns_generated = 0
    total_techniques_found = 0
    
    for stix_file in stix_files:
        print(f"\n📋 Processing {stix_file.name}...")
        objects = load_stix_objects(stix_file)
        
        if not objects:
            print(f"  ⚠️ No STIX objects found in {stix_file.name}")
            continue
        
        print(f"  📊 Loaded {len(objects)} STIX objects")
        
        # Extract all campaigns from this file
        campaigns = [obj for obj in objects if getattr(obj, "type", "") == "campaign"]
        
        print(f"  📋 Found {len(campaigns)} campaign(s) in {stix_file.name}")
        
        if not campaigns:
            print(f"  ⚠️ No campaigns found in {stix_file.name}")
            continue
        
        for campaign in campaigns:
            campaign_name = getattr(campaign, "name", "unknown")
            print(f"    🎯 Processing campaign: {campaign_name}")
            
            caldera_yaml = extract_campaign(campaign_name, objects)
            
            if not caldera_yaml:
                print(f"    ⚠️ Failed to extract campaign data for {campaign_name}")
                continue
            
            if not caldera_yaml.get('abilities'):
                print(f"    ⚠️ Campaign '{campaign_name}' has no techniques/abilities. Skipping.")
                continue
            
            # Generate safe filename
            safe_name = re.sub(r'[^\w\-_\. ]', '_', campaign_name)
            safe_name = safe_name.replace(' ', '_').lower()
            
            # Ensure filename is not too long
            if len(safe_name) > 50:
                safe_name = safe_name[:50]
            
            # Add "0." prefix to filename
            output_file = CALDERA_ADVERSARIES_DIR / f"0.{safe_name}.yml"
            
            # Check if file already exists and increment if needed
            counter = 1
            base_name = output_file.stem
            while output_file.exists():
                output_file = CALDERA_ADVERSARIES_DIR / f"{base_name}_{counter}.yml"
                counter += 1
            
            try:
                if save_campaign(output_file, caldera_yaml):
                    total_campaigns_generated += 1
                    abilities_count = len(caldera_yaml.get('abilities', {}))
                    total_techniques_found += abilities_count
                    
                    # Print campaign stats
                    print(f"    📈 Generated with {abilities_count} techniques")
            except Exception as e:
                print(f"    ❌ Failed to save campaign {campaign_name}: {e}")
    
    print(f"\n{'='*60}")
    print(f"🎉 Campaign generation completed!")
    print(f"   Total campaigns generated: {total_campaigns_generated}")
    print(f"   Total techniques across all campaigns: {total_techniques_found}")
    if total_campaigns_generated > 0:
        print(f"   Average techniques per campaign: {total_techniques_found/total_campaigns_generated:.1f}")
    print(f"   Output directory: {CALDERA_ADVERSARIES_DIR}")
    print(f"{'='*60}")

def upload_campaign(campaign_file: Path):
    """
    Uploads a single campaign YAML file to the Caldera server.
    Returns True if successful, False otherwise.
    """
    if not campaign_file.exists():
        print(f"❌ Campaign file not found: {campaign_file}")
        return False

    try:
        with open(campaign_file, "r", encoding="utf-8") as f:
            campaign_data = yaml.safe_load(f)

        url = f"{CALDERA_URL}/api/v2/adversaries"
        response = requests.post(url, headers=HEADERS, json=campaign_data)

        if response.status_code in (200, 201):
            print(f"✅ Uploaded campaign: {campaign_file.name}")
            return True
        else:
            print(f"❌ Failed to upload {campaign_file.name}: {response.status_code} - {response.text}")
            return False

    except yaml.YAMLError as e:
        print(f"❌ Invalid YAML in {campaign_file}: {e}")
        return False
    except Exception as e:
        print(f"❌ Error uploading {campaign_file}: {e}")
        return False

def upload_all_campaigns():
    """
    Uploads all campaign YAML files in CALDERA_ADVERSARIES_DIR,
    but only if they contain valid campaign data and have "0." prefix.
    """
    if not CALDERA_ADVERSARIES_DIR or not CALDERA_ADVERSARIES_DIR.exists():
        print(f"❌ Campaigns directory not found: {CALDERA_ADVERSARIES_DIR}")
        return

    # Only process files with "0." prefix
    files = list(CALDERA_ADVERSARIES_DIR.glob("0.*.yaml")) + list(CALDERA_ADVERSARIES_DIR.glob("0.*.yml"))
    
    if not files:
        print(f"⚠ No YAML files with '0.' prefix found in {CALDERA_ADVERSARIES_DIR}")
        return

    print(f"🔍 Checking {len(files)} YAML file(s) with '0.' prefix...")
    
    valid_campaigns = []
    invalid_files = []
    
    # First, identify which files contain valid campaign data
    for i, campaign_file in enumerate(files, 1):
        print(f"  [{i}/{len(files)}] Checking {campaign_file.name}...", end="")
        
        if not campaign_file.exists():
            print(" ❌ File not found")
            invalid_files.append((campaign_file, "File not found"))
            continue
        
        try:
            with open(campaign_file, "r", encoding="utf-8") as f:
                campaign_data = yaml.safe_load(f)
            
            # Check if this is a valid campaign file
            # Caldera campaigns typically have these fields
            is_valid_campaign = True
            issues = []
            
            if not isinstance(campaign_data, dict):
                is_valid_campaign = False
                issues.append("Not a valid YAML dictionary")
            else:
                # Check for required campaign fields
                required_fields = ["id", "name", "atomic_ordering", "abilities"]
                for field in required_fields:
                    if field not in campaign_data:
                        is_valid_campaign = False
                        issues.append(f"Missing required field: {field}")
                
                # Additional validation
                if "atomic_ordering" in campaign_data:
                    if not isinstance(campaign_data["atomic_ordering"], list):
                        is_valid_campaign = False
                        issues.append("atomic_ordering must be a list")
                
                if "abilities" in campaign_data:
                    if not isinstance(campaign_data["abilities"], dict):
                        is_valid_campaign = False
                        issues.append("abilities must be a dictionary")
            
            if is_valid_campaign:
                valid_campaigns.append((campaign_file, campaign_data))
                print(" ✅ Valid campaign")
            else:
                invalid_files.append((campaign_file, ", ".join(issues)))
                print(f" ❌ Not a valid campaign ({', '.join(issues)})")
                
        except yaml.YAMLError as e:
            invalid_files.append((campaign_file, f"Invalid YAML: {str(e)}"))
            print(f" ❌ Invalid YAML")
        except Exception as e:
            invalid_files.append((campaign_file, f"Error reading file: {str(e)}"))
            print(f" ❌ Error reading file")
    
    # Now upload only valid campaigns
    print(f"\n📤 Uploading {len(valid_campaigns)} valid campaign(s) with '0.' prefix...")
    
    if not valid_campaigns:
        print("⚠ No valid campaigns with '0.' prefix found to upload")
        
        # Show invalid files if any
        if invalid_files:
            print(f"\n📋 Invalid/non-campaign files found:")
            for file_path, reason in invalid_files:
                print(f"  • {file_path.name}: {reason}")
        return
    
    success_count = 0
    for i, (campaign_file, campaign_data) in enumerate(valid_campaigns, 1):
        print(f"  [{i}/{len(valid_campaigns)}] Uploading {campaign_file.name}...")
        if upload_campaign(campaign_file):
            success_count += 1
    
    # Summary
    print(f"\n{'='*60}")
    print(f"📊 Upload Summary")
    print(f"   Valid campaigns with '0.' prefix found: {len(valid_campaigns)}")
    print(f"   Successfully uploaded: {success_count}/{len(valid_campaigns)}")
    
    if invalid_files:
        print(f"\n⚠ Skipped {len(invalid_files)} invalid/non-campaign files:")
        for file_path, reason in invalid_files:
            print(f"  • {file_path.name}: {reason}")
    
    print(f"{'='*60}")

def list_campaigns():
    """
    Lists all campaigns (adversaries) stored in the Caldera server.
    """
    if not API_KEY:
        print("❌ API_KEY not configured in config.py")
        return
    
    try:
        url = f"{CALDERA_URL}/api/v2/adversaries"
        response = requests.get(url, headers=HEADERS)

        if response.status_code != 200:
            print(f"❌ Failed to list campaigns: {response.status_code} - {response.text}")
            return

        campaigns = response.json()
        if not campaigns:
            print("⚠ No campaigns found on the server.")
            return

        print(f"📋 Found {len(campaigns)} campaign(s) on Caldera server:")
        print(f"{'='*80}")
        for i, camp in enumerate(campaigns, 1):
            camp_id = camp.get("adversary_id", "unknown")
            name = camp.get("name", "Unnamed")
            description = camp.get("description", "")
            abilities = len(camp.get("abilities", []))
            
            print(f"{i:3d}. {name}")
            print(f"     ID: {camp_id}")
            print(f"     Techniques: {abilities}")
            if description and description != "No description available":
                desc_preview = description[:100] + "..." if len(description) > 100 else description
                print(f"     Description: {desc_preview}")
            print()
        print(f"{'='*80}")

    except requests.exceptions.ConnectionError:
        print(f"❌ Cannot connect to Caldera server at {CALDERA_URL}")
        print("   Make sure Caldera is running and the URL is correct.")
    except Exception as e:
        print(f"❌ Error listing campaigns: {e}")

def show_campaign_stats():
    """
    Show statistics about campaigns in CALDERA_ADVERSARIES_DIR.
    """
    if not CALDERA_ADVERSARIES_DIR or not CALDERA_ADVERSARIES_DIR.exists():
        print(f"❌ Campaigns directory not found: {CALDERA_ADVERSARIES_DIR}")
        return
    
    # Only show files with "0." prefix
    files = list(CALDERA_ADVERSARIES_DIR.glob("0.*.yaml")) + list(CALDERA_ADVERSARIES_DIR.glob("0.*.yml"))
    
    print(f"\n📊 Local Campaign Statistics (files with '0.' prefix)")
    print(f"{'='*60}")
    print(f"   Directory: {CALDERA_ADVERSARIES_DIR}")
    print(f"   Total campaign files with '0.' prefix: {len(files)}")
    
    if files:
        total_techniques = 0
        print(f"\n📋 Available campaigns with '0.' prefix:")
        for i, camp_file in enumerate(files, 1):
            try:
                with open(camp_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    name = data.get("name", "Unknown")
                    abilities = len(data.get("abilities", {}))
                    total_techniques += abilities
                    print(f"  {i:3d}. {name} ({abilities} techniques)")
            except Exception as e:
                print(f"  {i:3d}. {camp_file.name} (error reading: {e})")
        
        avg_techniques = total_techniques / len(files) if files else 0
        print(f"\n📈 Summary for '0.' prefixed campaigns:")
        print(f"   Total techniques across all campaigns: {total_techniques}")
        print(f"   Average techniques per campaign: {avg_techniques:.1f}")
    
    print(f"{'='*60}")

def show_help():
    """
    Prints available options for this script.
    """
    print("""
📌 Usage: python campaign.py <command> [args]

Commands:
  generate           Generate campaigns from STIX files (adds "0." prefix)
  upload <file>      Upload a specific campaign YAML file
  upload_all         Upload all campaigns with "0." prefix from CALDERA_ADVERSARIES_DIR
  list               List all campaigns from the Caldera server
  stats              Show statistics about generated campaigns with "0." prefix
  help               Show this help message

Examples:
  python campaign.py generate
  python campaign.py upload 0.solarwinds_campaign.yml
  python campaign.py upload_all
  python campaign.py list
  python campaign.py stats
  python campaign.py help

Configuration (in config.py):
  STIX_FILE      Path to STIX file or directory
  CALDERA_ADVERSARIES_DIR  Output directory for generated campaigns
  CALDERA_URL         URL of Caldera server
  CALDERA_API_KEY_RED API key for Caldera

Note: Generated campaigns will have "0." prefix in both filename and campaign name.
""")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_help()
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "generate":
        print("🔄 Generating campaigns from STIX files (with '0.' prefix)...")
        generate_campaigns()

    elif cmd == "upload":
        if len(sys.argv) < 3:
            print("❌ Please specify a file to upload")
            print("Usage: python campaign.py upload <filename>")
            sys.exit(1)
        file_path = Path(sys.argv[2])
        upload_campaign(file_path)

    elif cmd == "upload_all":
        print("🔄 Uploading all campaigns with '0.' prefix...")
        upload_all_campaigns()

    elif cmd == "list":
        print("📋 Listing campaigns from CALDERA server...")
        list_campaigns()

    elif cmd == "stats":
        print("📊 Showing campaign statistics (for '0.' prefixed files)...")
        show_campaign_stats()

    elif cmd in ["help", "-h", "--help"]:
        show_help()

    else:
        print(f"❌ Unknown command: {cmd}")
        show_help()
        sys.exit(1)
