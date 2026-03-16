#!/usr/bin/env python3
"""
Simplified version for splitting Caldera YAML - saves in current directory
"""
import yaml
import json
import sys
import os

def split_caldera_yaml(input_file):
    """Split Caldera YAML into ability and adversary JSON files in data/campaign/ directory."""
    
    with open(input_file, 'r') as f:
        data = yaml.safe_load(f)
    
    # Set output directory to data/campaign/
    output_dir = os.path.join(os.getcwd(), 'data', 'campaign')
    
    # Create directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Extract base name for output files
    base_name = os.path.splitext(os.path.basename(input_file))[0]
    
    # Extract abilities and convert to list format
    abilities_dict = data.get('abilities', {})
    # Convert dictionary to list of ability objects
    abilities_list = list(abilities_dict.values()) if isinstance(abilities_dict, dict) else abilities_dict
    
    # Process each ability
    issues = []
    for i, ability in enumerate(abilities_list):
        # Keep the "attack-pattern--" prefix in id (don't remove it)
        # Just ensure ability_id exists (copy from id if needed)
        if 'id' in ability and 'ability_id' not in ability:
            ability['ability_id'] = ability['id']
        
        # Rename technique_id to attack_id in technique object
        if 'technique' in ability and isinstance(ability['technique'], dict):
            if 'technique_id' in ability['technique']:
                ability['technique']['attack_id'] = ability['technique'].pop('technique_id')
        
        # Fix executors structure - flatten nested shell type
        if 'executors' in ability and isinstance(ability['executors'], list):
            flattened_executors = []
            for executor in ability['executors']:
                if isinstance(executor, dict):
                    # Check if it's the nested format: {"sh": {"platform": "linux", "command": "..."}}
                    for shell_name, shell_data in executor.items():
                        if isinstance(shell_data, dict) and 'platform' in shell_data:
                            # Flatten it to: {"name": "sh", "platform": "linux", "command": "..."}
                            flattened = {'name': shell_name}
                            flattened.update(shell_data)
                            flattened_executors.append(flattened)
                        else:
                            # Already flat or unknown format, keep as-is
                            flattened_executors.append(executor)
                else:
                    flattened_executors.append(executor)
            ability['executors'] = flattened_executors
        
        # Validate required fields
        if 'name' not in ability or not ability['name']:
            issues.append(f"Ability #{i} (ID: {ability.get('id', 'unknown')}) is missing 'name' field")
        if 'id' not in ability and 'ability_id' not in ability:
            issues.append(f"Ability #{i} (Name: {ability.get('name', 'unknown')}) is missing 'id' field")
    
    if issues:
        print("\n⚠️  WARNING: Found issues with abilities:")
        for issue in issues:
            print(f"  - {issue}")
        print()
    
    ability_file = os.path.join(output_dir, f"{base_name}_ability.json")
    with open(ability_file, 'w') as f:
        # Save as a list directly (not wrapped in {"abilities": ...})
        json.dump(abilities_list, f, indent=2)
    
    # Extract adversary (excluding abilities)
    adversary_data = {k: v for k, v in data.items() if k != 'abilities'}
    adversary_file = os.path.join(output_dir, f"{base_name}_adversary.json")
    with open(adversary_file, 'w') as f:
        json.dump(adversary_data, f, indent=2)
    
    print(f"Split complete. Files saved in: {output_dir}")
    print(f"  - {len(abilities_list)} abilities -> {os.path.basename(ability_file)}")
    print(f"  - Adversary data -> {os.path.basename(adversary_file)}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python split_caldera.py <caldera_file.yml>")
        print("Output: Creates <filename>_ability.json and <filename>_adversary.json in data/campaign/ directory")
        sys.exit(1)
    
    split_caldera_yaml(sys.argv[1])
