#!/usr/bin/env python3
"""
Convert campaign JSON to Caldera ability and adversary files.

Usage:
    python3 convert_to_caldera.py input_file.json

This will generate:
    input_file-ability.json   - Caldera abilities file
    input_file-adversary.json - Caldera adversary profile file
"""

import json
import sys
import os
import re
import uuid
from pathlib import Path
from typing import Dict, List, Any


def load_campaign_file(filepath: str) -> Dict[str, Any]:
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def generate_ability_id(technique_id: str, index: int) -> str:
    namespace = uuid.UUID('12345678-1234-5678-1234-567812345678')
    return str(uuid.uuid5(namespace, f"{technique_id}_{index}"))


def generate_adversary_id(campaign_name: str) -> str:
    namespace = uuid.UUID('87654321-4321-8765-4321-876543210987')
    return str(uuid.uuid5(namespace, campaign_name))


def clean_filename(filename: str) -> str:
    basename = os.path.basename(filename)
    dirname  = os.path.dirname(filename)
    cleaned  = re.sub(r'^\d+\.', '', basename)
    return os.path.join(dirname, cleaned) if dirname else cleaned


def create_ability_from_node(node: Dict[str, Any], campaign_name: str) -> Dict[str, Any]:
    technique_id   = node.get("technique_id", "T0000")
    technique_name = node.get("technique_name", "Unknown Technique")
    index          = node.get("node_index", 0)
    ability_name   = f"{technique_id} - {technique_name}".strip() or f"Ability {index}"

    raw_commands = node.get("attacker_commands", [])
    command_str  = "\n".join(raw_commands) if raw_commands else "echo 'no command specified'"

    source = node.get("source_host_type", "linux").lower()
    if source in ("windows", "win"):
        platform      = "windows"
        executor_name = "psh"
    else:
        platform      = "linux"
        executor_name = "sh"

    return {
        "ability_id":     generate_ability_id(technique_id, index),
        "name":           ability_name,
        "tactic":         node.get("tactic", ""),
        "technique_name": technique_name,
        "technique_id":   technique_id,
        "description":    node.get("campaign_context") or node.get("description", ""),
        "executors": [
            {
                "name":     executor_name,
                "platform": platform,
                "command":  command_str,
            }
        ],
    }


def create_adversary_from_campaign(campaign_data: Dict[str, Any]) -> Dict[str, Any]:
    campaign_name = campaign_data["campaign_name"]

    sorted_nodes    = sorted(campaign_data["structural_nodes"], key=lambda x: x["node_index"])
    atomic_ordering = [
        generate_ability_id(node["technique_id"], node["node_index"])
        for node in sorted_nodes
    ]

    return {
        "id":              generate_adversary_id(campaign_name),
        "name":            campaign_name,
        "description":     (
            f"{campaign_name} emulation plan with {len(sorted_nodes)} techniques"
        ),
        "atomic_ordering": atomic_ordering,
    }


def generate_ability_file(campaign_data: Dict[str, Any], output_file: str):
    abilities = [
        create_ability_from_node(node, campaign_data["campaign_name"])
        for node in campaign_data["structural_nodes"]
    ]

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(abilities, f, indent=2, ensure_ascii=False)

    print(f"✓ Ability file: {len(abilities)} abilities → {output_file}")


def generate_adversary_file(campaign_data: Dict[str, Any], output_file: str):
    adversary = create_adversary_from_campaign(campaign_data)

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(adversary, f, indent=2, ensure_ascii=False)

    print(f"✓ Adversary file: {len(adversary['atomic_ordering'])} techniques → {output_file}")


def validate_campaign_data(campaign_data: Dict[str, Any]) -> bool:
    for field in ["campaign_name", "structural_nodes", "metadata", "dag_representation"]:
        if field not in campaign_data:
            print(f"✗ Missing required field: '{field}'")
            return False

    if not campaign_data["structural_nodes"]:
        print("✗ No structural nodes found")
        return False

    if campaign_data["metadata"]["validation"].get("is_dag") is False:
        print("✗ Campaign data contains cycles (not a valid DAG)")
        return False

    total  = campaign_data["metadata"]["total_techniques"]
    is_dag = campaign_data["metadata"]["validation"]["is_dag"]
    print(f"✓ Validated: {campaign_data['campaign_name']} | techniques={total} | is_dag={is_dag}")
    return True


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 convert_to_caldera.py <campaign_file.json>")
        sys.exit(1)

    input_file = sys.argv[1]

    if not os.path.exists(input_file):
        print(f"✗ File not found: '{input_file}'")
        sys.exit(1)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config.config import CALDERA_API_FILES
    CALDERA_API_FILES.mkdir(parents=True, exist_ok=True)

    base_name    = os.path.splitext(os.path.basename(input_file))[0]
    cleaned_base = clean_filename(base_name)

    if cleaned_base != base_name:
        print(f"ℹ Prefix removed: '{base_name}' → '{cleaned_base}'")

    ability_file   = str(CALDERA_API_FILES / f"{cleaned_base}-ability.json")
    adversary_file = str(CALDERA_API_FILES / f"{cleaned_base}-adversary.json")

    try:
        print(f"Loading: {input_file}")
        campaign_data = load_campaign_file(input_file)

        if not validate_campaign_data(campaign_data):
            sys.exit(1)

        generate_ability_file(campaign_data, ability_file)
        generate_adversary_file(campaign_data, adversary_file)

        dag = campaign_data["dag_representation"]
        print("\n✨ Done!")
        print(f"  Abilities : {ability_file}")
        print(f"  Adversary : {adversary_file}")
        print(f"\n📊 {campaign_data['campaign_name']}")
        print(f"  Techniques : {campaign_data['metadata']['total_techniques']}")
        print(f"  Root       : {dag['roots'][0]}")
        print(f"  Leaf       : {dag['leaves'][0]}")

    except json.JSONDecodeError as e:
        print(f"✗ Invalid JSON: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
