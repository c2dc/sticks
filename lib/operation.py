import sys
import json
import requests
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    import config
except ImportError:
    print("❌ Could not import 'config' module. Make sure your PYTHONPATH includes the project root.")
    sys.exit(1)

HEADERS = {
    "Content-Type": "application/json",
    "KEY": config.CALDERA_API_KEY_RED
}

BASE_URL = config.CALDERA_URL.rstrip("/")

def list_operations():
    """List all operations with selected fields"""
    endpoint = f"{BASE_URL}/api/v2/operations"
    try:
        resp = requests.get(endpoint, headers=HEADERS)
        resp.raise_for_status()
        ops = resp.json()
        #print("📄 Operations:")
        #print(json.dumps(ops, indent=2))
        filtered_ops = []
        for op in ops:
            filtered_op = {
                "name": op.get("name"),
                "state": op.get("state"),
                "start": op.get("start"),
                "group": op.get("group"),
                "steps": op.get("steps", [])
            }
            filtered_ops.append(filtered_op)

        print("📄 Operations:")
        print(json.dumps(filtered_ops, indent=2))
        return filtered_ops

    except requests.RequestException as e:
        print(f"❌ Failed to list operations: {e}")
        sys.exit(1)

def get_operation(op_id):
    """Get details of a single operation by ID"""
    endpoint = f"{BASE_URL}/api/v2/operations/{op_id}"
    try:
        resp = requests.get(endpoint, headers=HEADERS)
        resp.raise_for_status()
        op_data = resp.json()
        print(f"📄 Operation {op_id} details:")
        print(json.dumps(op_data, indent=2))
        return op_data
    except requests.RequestException as e:
        print(f"❌ Failed to get operation {op_id}: {e}")
        sys.exit(1)


def adversary_exists(adversary_id):
    """Check if the given adversary ID exists on the server"""
    endpoint = f"{BASE_URL}/api/v2/adversaries"
    try:
        resp = requests.get(endpoint, headers=HEADERS)
        resp.raise_for_status()
        adversaries = resp.json()
        for adv in adversaries:
            if adv.get("adversary_id") == adversary_id:
                print("*********************")
                return True
        return False
    except requests.RequestException as e:
        print(f"❌ Failed to fetch adversaries: {e}")
        sys.exit(1)


def group_has_agents(group_name):
    """Check if the group has at least one agent"""
    endpoint = f"{BASE_URL}/api/v2/agents"
    try:
        resp = requests.get(endpoint, headers=HEADERS)
        resp.raise_for_status()
        agents = resp.json()
        # Filter agents by group name
        group_agents = [a for a in agents if a.get("group") == group_name]
        return len(group_agents) > 0
    except requests.RequestException as e:
        print(f"❌ Failed to fetch agents: {e}")
        sys.exit(1)


def create_operation(name, adversary_id=None, group="", planner="batch", jitter="2/8", **kwargs):
    """Create a new operation after validations"""
    
    if adversary_id and not adversary_exists(adversary_id):
        print(f"❌ Adversary ID '{adversary_id}' does not exist on the server.")
        sys.exit(1)
    
    if not group_has_agents(group):
        print(f"❌ No agents found in group '{group}'. Cannot create operation.")
        sys.exit(1)

    endpoint = f"{BASE_URL}/api/rest"
    payload = {
        "index": "operations",
        "name": name,
        "group": group,
        "planner": planner,
        "jitter": jitter
    }
    if adversary_id:
        payload["adversary_id"] = adversary_id
    payload.update(kwargs)

    print(f"📡 Creating operation '{name}' with payload:")
    print(json.dumps(payload, indent=2))

    try:
        resp = requests.put(endpoint, headers=HEADERS, json=payload)
        resp.raise_for_status()
        print("✅ Operation created successfully!")
        return resp.json()
    except requests.RequestException as e:
        print(f"❌ Failed to create operation: {e}")
        sys.exit(1)


def remove_operation(op_id):
    """Remove an operation by ID"""
    endpoint = f"{BASE_URL}/api/v2/operations/{op_id}"
    try:
        resp = requests.delete(endpoint, headers=HEADERS)
        resp.raise_for_status()
        print(f"🗑️ Operation {op_id} removed successfully.")
        return resp.json()
    except requests.RequestException as e:
        print(f"❌ Failed to remove operation {op_id}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage Caldera operations")
    parser.add_argument("action", choices=["list", "get", "create", "remove"], help="Action to perform")
    parser.add_argument("--id", help="Operation ID (for get/remove)")
    parser.add_argument("--name", help="Operation name (for create)")
    parser.add_argument("--adversary", help="Adversary ID (for create)")
    parser.add_argument("--group", default="", help="Operation group (optional, for create)")
    parser.add_argument("--planner", default="batch", help="Planner type (for create)")
    parser.add_argument("--jitter", default="2/8", help="Jitter (for create)")
    args = parser.parse_args()

    if args.action == "list":
        list_operations()
    elif args.action == "get":
        if not args.id:
            print("❌ --id is required for 'get'")
            sys.exit(1)
        get_operation(args.id)
    elif args.action == "create":
        if not args.name:
            print("❌ --name is required for 'create'")
            sys.exit(1)
        if not args.group:
            print("❌ --group is required for 'create'")
        if not args.adversary:
            print("❌ --group is required for 'create'")            
        create_operation(name=args.name, adversary_id=args.adversary, group=args.group, planner=args.planner, jitter=args.jitter )
    elif args.action == "remove":
        if not args.id:
            print("❌ --id is required for 'remove'")
            sys.exit(1)
        remove_operation(args.id)