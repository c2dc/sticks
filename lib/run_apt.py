import requests
import json
from config import config

CALDERA_URL = config.CALDERA_URL.rstrip('/')
API_KEY = config.CALDERA_API_KEY_RED

HEADERS = {
    "Content-Type": "application/json",
    "KEY": API_KEY
}

def test_sources():
    url = f"{CALDERA_URL}/api/v2/sources"
    response = requests.get(url, headers=HEADERS, verify=False)
    if response.ok:
        print("Sources endpoint OK")
    else:
        print(f"Sources endpoint failed: {response.status_code} - {response.text}")

def fetch_agents():
    url = f"{CALDERA_URL}/api/v2/agents"
    response = requests.get(url, headers=HEADERS, verify=False)
    if response.ok:
        agents = response.json()
        print(f"Agents list:\n{json.dumps(agents, indent=2)}")
    else:
        print(f"Failed to fetch agents: {response.status_code} - {response.text}")

def main():
    print("Testing connection to Caldera API...")
    test_sources()
    fetch_agents()

if __name__ == "__main__":
    main()
