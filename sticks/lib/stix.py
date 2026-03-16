# lib/stix.py

from pathlib import Path
import requests
import sys
from urllib.parse import urlparse
import os
import json
import ijson
from stix2 import parse, Bundle
from pathlib import Path
import sys
from typing import List, Dict, Any

project_root = Path(__file__).resolve().parent.parent  # parent of lib/
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Import config.py from config directory
try:
    import config
except ImportError:
    print("❌ Could not import 'config' module. Make sure your PYTHONPATH includes the project root.")
    sys.exit(1)


def get_filename_from_url(url: str) -> str:
    path = urlparse(url).path
    return os.path.basename(path)

def ensure_dir(directory: Path):
    if not directory.exists():
        directory.mkdir(parents=True)
        print(f"📁 Created directory: {directory}")

def download_file(url: str, directory: Path, filename: str):
    filepath = directory / filename
    print(f"⬇️ Downloading {url} ...")
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        with open(filepath, 'wb') as f:
            f.write(response.content)
        print(f"✅ Saved to {filepath}")
    except requests.RequestException as e:
        print(f"❌ Failed to download file: {e}")

def download_all():
    url = config.STIX_URL
    download_dir = config.STIX_DIR

    filename = get_filename_from_url(url)

    ensure_dir(download_dir)
    download_file(url, download_dir, filename)


def load_stix_data(file_path: Path) -> List[Any]:
    try:
        with open(file_path, "r", encoding='utf-8') as f:
            raw_data = json.load(f)
    except FileNotFoundError:
        print(f"❌ File '{file_path}' not found.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in '{file_path}': {e}")
        sys.exit(1)

    parsed_objects = []
    skipped = 0

    for i, obj in enumerate(raw_data.get("objects", [])):
        try:
            parsed = parse(obj, allow_custom=True)
            if hasattr(parsed, "type"):
                parsed_objects.append(parsed)
            else:
                skipped += 1
        except Exception as e:
            print(f"Skipping object {i} due to error: {e}")
            skipped += 1

    print(f"✅ Parsed {len(parsed_objects)} objects. Skipped {skipped}.")
    return parsed_objects


def get_related_objects(objects: List[Any], source_id: str) -> tuple[List[Any], List[Any]]:
    relationships = [
        obj for obj in objects
        if (getattr(obj, "type", "") == "relationship" and 
            getattr(obj, "source_ref", "") == source_id)
    ]
    target_ids = {getattr(rel, "target_ref", "") for rel in relationships}
    targets = [obj for obj in objects if getattr(obj, "id", "") in target_ids]

    reverse_relationships = [
        obj for obj in objects
        if (getattr(obj, "type", "") == "relationship" and 
            getattr(obj, "target_ref", "") == source_id)
    ]
    reverse_source_ids = {getattr(rel, "source_ref", "") for rel in reverse_relationships}
    reverse_sources = [obj for obj in objects if getattr(obj, "id", "") in reverse_source_ids]

    all_relationships = relationships + reverse_relationships
    all_targets = targets + reverse_sources

    return all_relationships, all_targets

def extract_all_apts():
    config.APT_DIR.mkdir(parents=True, exist_ok=True)
    objects = load_stix_data(config.STIX_FILE)
    intrusion_sets = [obj for obj in objects if getattr(obj, "type", "") == "intrusion-set"]

    if not intrusion_sets:
        print("⚠️ No intrusion sets found in STIX data.")
        return

    print(f"🎯 Found {len(intrusion_sets)} intrusion sets. Extracting...")

    i=1
    for  apt_group in intrusion_sets:
        group_name = getattr(apt_group, "name", "unknown_apt").replace(" ", "_").replace("/", "_")
        group_id = getattr(apt_group, "id", "unknown_id")
        filename = f"{group_name}_{group_id}.json"

        relationships, targets = get_related_objects(objects, group_id)

        seen_ids = set()
        unique_objects = []

        for obj in [apt_group] + relationships + targets:
            obj_id = getattr(obj, "id", "")
            if obj_id and obj_id not in seen_ids:
                seen_ids.add(obj_id)
                unique_objects.append(obj)

        try:
            bundle = Bundle(*unique_objects, allow_custom=True)
        except Exception as e:
            print(f"❌ Failed to create bundle for {group_name}: {e}")
            continue

        try:
            with open(config.APT_DIR / filename, "w", encoding='utf-8') as f:
                f.write(bundle.serialize(pretty=True))
            print(f"✅ Saved ({i}/{len(intrusion_sets)}) {filename} with {len(bundle.objects)} objects.")
            i+=1
        except Exception as e:
            print(f"❌ Failed to write {filename}: {e}")


def get_stix_files(directory: Path, exclude_file: Path) -> List[Path]:
    return [
        f for f in directory.glob("*.json")
        if f.name != exclude_file.name
    ]

def load_stix_objects_streaming(file_path: Path) -> List[Dict]:
    print(f"📥 Streaming load from {file_path.name}...")
    objects = []
    count = 0
    with open(file_path, "rb") as f:
        try:
            for obj in ijson.items(f, "objects.item"):
                objects.append(obj)
                count += 1
        except ijson.JSONError as e:
            print(f"❌ Error parsing {file_path.name}: {e}")
    print(f"🔢 Found {count} objects in {file_path.name}")
    return objects

def write_bundle(objects: List[dict], output_file: Path):
    bundle = Bundle(objects=objects, allow_custom=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(bundle.serialize(pretty=True))
    print(f"✅ Merged STIX written to {output_file}")

def merge_all_stix_files():
    stix_dir = config.STIX_DIR
    merged_file = config.STIX_FILE

    stix_files = get_stix_files(stix_dir, merged_file)

    if not stix_files:
        print("⚠️ No STIX files to process.")
        return

    all_objects = []
    for stix_file in stix_files:
        all_objects.extend(load_stix_objects_streaming(stix_file))

    print(f"📊 Total objects loaded (before deduplication): {len(all_objects)}")

    if len(stix_files) == 1:
        print("ℹ️ Only one STIX file found. Copying it directly without rebundling.")
        import shutil
        shutil.copy2(stix_files[0], merged_file)
        print(f"✅ Copied {stix_files[0].name} to {merged_file.name}")
        return

    # Remove duplicates by ID
    seen_ids = set()
    unique_objects = []
    for obj in all_objects:
        obj_id = obj.get("id")
        if obj_id and obj_id not in seen_ids:
            seen_ids.add(obj_id)
            unique_objects.append(obj)

    print(f"🧩 Total unique objects collected: {len(unique_objects)}")
    write_bundle(unique_objects, merged_file)


def show_help():
    """Prints the usage instructions for the script."""
    print("""
📌 Usage: python lib/stix.py <command>

This script helps download, process, and manage STIX threat intelligence data.

Available Commands:
  download      Downloads the main STIX bundle from the URL in config.py.
  extract       Parses the main STIX file and extracts adversaries
                (intrusion-set) into its own separate JSON bundle file.
  merge         Merges all STIX JSON files in STIX_DIR into a single bundle.
  help          Shows this help message.

Example:
  python lib/stix.py download
  python lib/stix.py merge
  python lib/stix.py extract
""")

def main():
    """Main function to drive the script from the command line."""
    if len(sys.argv) < 2:
        print("❌ No command provided.")
        show_help()
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "download":
        print("🚀 Starting STIX data download...")
        download_all()
    elif command == "merge":
        print("🚀 Starting STIX file merge...")
        merge_all_stix_files()
    elif command == "extract":
        print("🚀 Starting extraction of adversaries...")
        extract_all_apts()
    elif command == "help":
        show_help()
    else:
        print(f"❌ Unknown command: '{command}'")
        show_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
