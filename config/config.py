# config.py
from pathlib import Path

# Git hub token to download files
GITHUB_TOKEN = "XXXXXXXXXXXXXXXXXX"


# STIX source URL
STIX_URL = "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/enterprise-attack/enterprise-attack.json"

# Base data directory
DATA_DIR = Path("data")

# Paths
ATOMIC_RED_DIR = DATA_DIR / "atomic-red"
STIX_DIR = DATA_DIR / "stix"        # All STIX files (including merged) live here
APT_DIR = DATA_DIR / "stix_adversaries"   # APT bundles go here
CALDERA_ABILITIES_DIR = DATA_DIR / "caldera_abilities" 
CALDERA_ADVERSARIES_DIR = DATA_DIR / "caldera_adversaries"


# Files 
STIX_FILE = STIX_DIR / "stix_full.json"

# Caldera Server
AGENT_PATHS = {
    "linux": "/tmp/master",
    "windows": "C:\\Temp\\master.exe",
    "darwin": "/tmp/master_mac"
}

CALDERA_URL = "http://localhost:8888"  # Adjust if needed
CALDERA_USERNAME = "red"
CALDERA_PASSWORD = "admin"
CALDERA_API_KEY_RED = "ADMIN123"
AGENT_PATH = DATA_DIR / "agents"