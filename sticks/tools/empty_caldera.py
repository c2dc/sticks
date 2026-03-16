import requests
from pathlib import Path
import sys
import json
from typing import List, Dict, Any
import requests
import argparse

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    import config
except ImportError:
    print("❌ Could not import 'config' module. Make sure your PYTHONPATH includes the project root.")
    sys.exit(1)
    
API_URL = "http://localhost:8888/api/v2"
API_KEY = config.CALDERA_API_KEY_RED

headers = {"KEY": API_KEY}


def delete_operations():
    """Delete all operations"""
    print("\n🔹 Deleting all operations...")
    
    resp = requests.get(f"{API_URL}/operations", headers=headers)
    if resp.status_code != 200:
        print(f"❌ Failed to fetch operations: {resp.status_code} - {resp.text}")
        return False
    
    operations = resp.json()
    if not operations:
        print("⚠️ No operations found.")
        return True
    
    success = True
    for op in operations:
        op_id = op["id"]
        op_name = op.get("name", "unknown")
        print(f"Deleting operation: {op_name} ({op_id})")

        del_resp = requests.delete(f"{API_URL}/operations/{op_id}", headers=headers)
        if del_resp.status_code in (200, 204):
            print(f"✅ Deleted {op_name} ({op_id})")
        else:
            print(f"❌ Failed {op_name} ({op_id}): {del_resp.status_code} - {del_resp.text}")
            success = False
    
    print("🎯 Operations deletion completed.")
    return success

def delete_abilities():
    """Delete all abilities"""
    print("\n🔹 Deleting all abilities...")
    
    resp = requests.get(f"{API_URL}/abilities", headers=headers)
    if resp.status_code != 200:
        print(f"❌ Failed to fetch abilities: {resp.status_code} - {resp.text}")
        return False
    
    abilities = resp.json()
    if not abilities:
        print("⚠️ No abilities found.")
        return True
    
    success = True
    for ability in abilities:
        ability_id = ability["ability_id"]
        print(f"Deleting ability: {ability_id}")
        del_resp = requests.delete(f"{API_URL}/abilities/{ability_id}", headers=headers)
        if del_resp.status_code in (200, 204):
            print(f"✅ Deleted {ability_id}")
        else:
            print(f"❌ Failed {ability_id}: {del_resp.status_code} - {del_resp.text}")
            success = False
    
    print("🎯 Abilities deletion completed.")
    return success

def delete_adversaries():
    """Delete all adversaries"""
    print("\n🔹 Deleting all adversaries...")
    
    resp = requests.get(f"{API_URL}/adversaries", headers=headers)
    if resp.status_code != 200:
        print(f"❌ Failed to fetch adversaries: {resp.status_code} - {resp.text}")
        return False
    
    adversaries = resp.json()
    if not adversaries:
        print("⚠️ No adversaries found.")
        return True
    
    success = True
    for adv in adversaries:
        adv_id = adv["adversary_id"]
        print(f"Deleting adversary: {adv_id}")
        del_resp = requests.delete(f"{API_URL}/adversaries/{adv_id}", headers=headers)
        if del_resp.status_code in (200, 204):
            print(f"✅ Deleted {adv_id}")
        else:
            print(f"❌ Failed {adv_id}: {del_resp.status_code} - {del_resp.text}")
            success = False
    
    print("🎯 Adversaries deletion completed.")
    return success

def main():
    parser = argparse.ArgumentParser(description="Delete Caldera operations, abilities, and adversaries")
    parser.add_argument(
        "--target", 
        "-t", 
        nargs="+", 
        choices=["all", "operation", "ability", "adversary"],
        default=["all"],
        help="Specify which items to delete (default: all)"
    )
    
    args = parser.parse_args()
    
    # Convert to set for easier checking
    targets = set(args.target)
    
    if "all" in targets:
        targets = {"operation", "ability", "adversary"}
    
    print(f"🎯 Targets to delete: {', '.join(targets)}")
    
    results = {}
    
    if "operation" in targets:
        results["operations"] = delete_operations()
    
    if "ability" in targets:
        results["abilities"] = delete_abilities()
    
    if "adversary" in targets:
        results["adversaries"] = delete_adversaries()
    
    # Summary
    print("\n" + "="*50)
    print("SUMMARY:")
    print("="*50)
    
    all_success = True
    for target, success in results.items():
        status = "✅ SUCCESS" if success else "❌ FAILED"
        print(f"{target.title():<12}: {status}")
        if not success:
            all_success = False
    
    if all_success:
        print(f"\n🎯 All requested deletions completed successfully!")
    else:
        print(f"\n⚠️  Some deletions failed. Check the logs above.")
        sys.exit(1)

if __name__ == "__main__":
    main()