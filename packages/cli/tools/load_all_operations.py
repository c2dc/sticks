import requests
from pathlib import Path
import sys
import subprocess
import time

# Setup paths
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Import config with validation
try:
    import config
    if not hasattr(config, 'CALDERA_API_KEY_RED'):
        print("❌ config.CALDERA_API_KEY_RED not found in config file")
        sys.exit(1)
    API_KEY = config.CALDERA_API_KEY_RED
except ImportError:
    print("❌ Could not import 'config' module. Make sure your PYTHONPATH includes the project root.")
    sys.exit(1)

# Check if operation script exists
operation_script = project_root / "lib" / "operation.py"
if not operation_script.exists():
    print(f"❌ Operation script not found at: {operation_script}")
    sys.exit(1)

# API configuration
API_URL = "http://localhost:8888/api/v2"
headers = {"KEY": API_KEY}

# 1️⃣ Fetch all adversary IDs from CALDERA
print("📡 Fetching adversaries from CALDERA...")
resp = requests.get(f"{API_URL}/adversaries", headers=headers)
if resp.status_code != 200:
    print(f"❌ Failed to fetch adversaries: {resp.status_code} - {resp.text}")
    exit(1)

adversaries = resp.json()  # v2 API returns a list of adversary objects

if not adversaries:
    print("⚠️ No adversaries found in CALDERA.")
    exit(0)

print(f"✅ Found {len(adversaries)} adversaries\n")

# 2️⃣ Filter out "Everything Bagel" adversary
adversaries_to_process = []
skipped_count = 0

for adv in adversaries:
    adv_name = adv.get("name", "")
    adv_id = adv["adversary_id"]
    
    if adv_name == "Everything Bagel":
        print(f"⏭️ Skipping adversary: {adv_name} (ID: {adv_id})")
        skipped_count += 1
    else:
        adversaries_to_process.append(adv)

print(f"\n📊 Processing {len(adversaries_to_process)} adversaries (skipped {skipped_count})\n")

# 3️⃣ Loop through filtered adversaries and create operations
success_count = 0
fail_count = 0

for count, adv in enumerate(adversaries_to_process, start=1):
    adv_id = adv["adversary_id"]
    adv_name = adv.get("name", "Unknown")
    op_name = f"OP{count:03d}"
    print(f"🔄 [{count}/{len(adversaries_to_process)}] Creating operation: {op_name} with adversary: {adv_name} (ID: {adv_id})")
    time.sleep(1)

    # Call CALDERA lib/operation.py CLI
    result = subprocess.run([
        "python3", str(operation_script), "create",
        "--name", op_name,
        "--group", "red",
        "--planner", "atomic",
        "--jitter", "2/8",
        "--adversary", adv_id
    ], capture_output=True, text=True)

    if result.returncode == 0:
        print(f"✅ Operation {op_name} created successfully.")
        success_count += 1
    else:
        print(f"❌ Failed to create {op_name}:\n{result.stderr}")
        fail_count += 1

    time.sleep(1)  # optional sleep between operations

# Summary
print(f"\n📊 Final Summary:")
print(f"   - Total adversaries found: {len(adversaries)}")
print(f"   - Skipped (Everything Bagel): {skipped_count}")
print(f"   - Processed: {len(adversaries_to_process)}")
print(f"   - Successfully created: {success_count}")
print(f"   - Failed: {fail_count}")
