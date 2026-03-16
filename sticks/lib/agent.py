from datetime import datetime, timezone, timedelta
import requests
import platform
import sys
import os
import subprocess
from pathlib import Path
import json

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
HEADERS = {
    "KEY": API_KEY,
    "Content-Type": "application/json"
}


def kill_agent(agent_paw):
    url = f"{CALDERA_URL}/api/v2/agents/{agent_paw}"
    payload = {
        "watchdog": 1,
        "sleep_min": 3,
        "sleep_max": 3  
    }
    try:
        response = requests.patch(url, headers=HEADERS, json=payload)
        response.raise_for_status()
        print(f"✅ Kill command sent to agent {agent_paw}")
    except requests.HTTPError as e:
        print(f"❌ Failed to send kill command: {e} - {response.text}")
    except Exception as e:
        print(f"❌ Error: {e}")


def list_agents():
    url = f"{CALDERA_URL}/api/v2/agents"
    try:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        agents = response.json()

        #print(f"✅ Found {len(agents)} agents:")
        #for i, agent in enumerate(agents):
        #    print(f"\nAgent #{i + 1} Full Data:")
        #    print(json.dumps(agent, indent=2))

        print("\nSummary:")

        def is_agent_alive(agent):
            last_seen_str = agent.get("last_seen")
            if not last_seen_str:
                return False  # Sem informação, assume morto

            # Converte ISO8601 para datetime
            last_seen = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)

            delta = now - last_seen
            return delta.total_seconds() < 60  # 1 minuto

        for agent in agents:
            agent_id = agent.get("paw") or agent.get("id") or agent.get("uuid") or "unknown"
            hostname = agent.get("host") or "unknown"
            platform = agent.get("platform") or "unknown"
            alive_status = "alive" if is_agent_alive(agent) else "dead"
            print(f"- ID: {agent_id}, Hostname: {hostname}, Platform: {platform}, Status: {alive_status}")

    except requests.HTTPError as e:
        print(f"❌ Failed to list agents: {e} - {response.text}")
    except Exception as e:
        print(f"❌ Error: {e}")

import os
from pathlib import Path

def remove_agent(agent_id):
    if not agent_id:
        print("❌ Agent ID não fornecido.")
        return

    # Primeiro buscar dados do agente para pegar a localização do arquivo
    get_url = f"{CALDERA_URL}/api/v2/agents/{agent_id}"
    try:
        get_response = requests.get(get_url, headers=HEADERS)
        get_response.raise_for_status()
        agent_data = get_response.json()
        location = agent_data.get("location")

        if location:
            filename = Path(location).name
            filepath = config.AGENT_PATH / filename
            if filepath.exists():
                try:
                    filepath.unlink()
                    print(f"✅ Arquivo local do agente removido: {filepath}")
                except Exception as e:
                    print(f"❌ Falha ao remover arquivo local {filepath}: {e}")
            else:
                print(f"⚠️ Arquivo local do agente não encontrado: {filepath}")
        else:
            print("⚠️ Campo 'location' não encontrado no agente. Pulando remoção de arquivo local.")

    except requests.HTTPError as e:
        print(f"❌ Falha ao obter dados do agente: {e} - {get_response.text}")
        return
    except Exception as e:
        print(f"❌ Erro ao obter dados do agente: {e}")
        return

    # Agora remove o agente no servidor Caldera
    del_url = f"{CALDERA_URL}/api/v2/agents/{agent_id}"
    try:
        del_response = requests.delete(del_url, headers=HEADERS)
        del_response.raise_for_status()
        print(f"✅ Agent {agent_id} removido com sucesso do servidor.")
    except requests.HTTPError as e:
        if del_response.status_code == 404:
            print(f"❌ Agent {agent_id} não encontrado no servidor.")
        else:
            print(f"❌ Falha ao remover agent do servidor: {e} - {del_response.text}")
    except Exception as e:
        print(f"❌ Erro ao remover agent do servidor: {e}")



def add_agent(platform_name, group_name, agent_name):
    agent_dir = config.AGENT_PATH  # Path object
    agent_dir.mkdir(parents=True, exist_ok=True)

    agent_file_path = agent_dir / agent_name
    server_url = CALDERA_URL

    sandcat_files = {
        "linux": "sandcat.go",
        "windows": "sandcat.go",
        "darwin": "sandcat.go"
    }
    sandcat_file = sandcat_files.get(platform_name.lower())
    if not sandcat_file:
        print(f"❌ Unsupported platform: {platform_name}")
        return

    download_url = f"{server_url}/file/download"
    headers = {
        "file": sandcat_file,
        "platform": platform_name.lower()
    }

    print(f"⬇️ Downloading Sandcat agent for platform '{platform_name}' from Caldera...")

    try:
        response = requests.post(download_url, headers=headers, verify=False)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ Failed to download Sandcat agent: {e}")
        return

    try:
        with open(agent_file_path, "wb") as f:
            f.write(response.content)
        print(f"✅ Sandcat agent saved to {agent_file_path}")
    except Exception as e:
        print(f"❌ Failed to save agent file: {e}")
        return

    if platform_name.lower() != "windows":
        try:
            os.chmod(agent_file_path, 0o755)
            print("✅ Set executable permissions on the agent binary.")
        except Exception as e:
            print(f"⚠️ Warning: Could not set executable permissions: {e}")

    print("▶️ Running the agent locally...")
    try:
        cmd = [
            str(agent_file_path),
            "-server", server_url,
            "-group", group_name,
            "-v"
        ]
        subprocess.Popen(cmd)
        print("✅ Agent launched successfully!")
    except Exception as e:
        print(f"❌ Failed to run the agent: {e}")

def print_usage():
    print(f"""
Usage:
    python agent.py list
        List all active agents.

    python agent.py add <platform> <group> <agent_name>
        Add a new agent (download sandcat and save with agent_name).

    python agent.py remove <agent_id>
        Remove an agent by its ID.

    python agent.py kill <agent_id>
        Send kill command to an agent to stop it.

    python agent.py help
        Show this help message.
""")

def main():
    if len(sys.argv) < 2:
        print("No command provided.\n")
        print_usage()
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "list":
        list_agents()

    elif cmd == "add":
        if len(sys.argv) < 5:
            print("❌ Missing arguments for add command.\n")
            print_usage()
            sys.exit(1)
        platform = sys.argv[2]
        group = sys.argv[3]
        agent_name = sys.argv[4]
        add_agent(platform, group, agent_name)

    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("❌ Missing agent ID for remove command.\n")
            print_usage()
            sys.exit(1)
        remove_agent(sys.argv[2])

    elif cmd == "kill":
        if len(sys.argv) < 3:
            print("❌ Missing agent ID for kill command.\n")
            print_usage()
            sys.exit(1)
        kill_agent(sys.argv[2])

    elif cmd == "help":
        print_usage()

    else:
        print(f"❌ Unknown command: {cmd}\n")
        print_usage()
        sys.exit(1)

if __name__ == "__main__":
    main()