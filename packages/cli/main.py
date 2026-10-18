import sys
import os
import argparse
from pathlib import Path
import subprocess

# Project root (where main.py lives)
project_root = Path(__file__).resolve().parent

# Add lib and config folders to sys.path
sys.path.insert(0, str(project_root / "lib"))
sys.path.insert(0, str(project_root / "config"))

# Import modules
try:
    import config
    import stix
    import agent
    import campaign
    import intrusionSet
    import ability
except ImportError as e:
    print(f"❌ Import error: {e}")
    sys.exit(1)


def ensure_dirs(*dirs):
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        print(f"📁 Ensured directory: {d}")


def main():
    parser = argparse.ArgumentParser(description="Run APT extraction pipeline or specific steps.")
    parser.add_argument(
        "step",
        choices=["init", "test", "help","clean"],
        nargs="?",
        default="help",
        help="Step to run (default: help)"
    )
    args = parser.parse_args()

    if args.step == "init":
        ensure_dirs(config.STIX_DIR, config.APT_DIR, config.AGENT_PATH, config.CALDERA_ABILITIES_DIR)

        print("\n⬆️ *** Stage 1")
        print("\n📥 Downloading STIX data...")
        stix.download_all()

        print("\n📥 Downloading Atomic Red Team data...")
        ability.get_atomic()

        print("\n🔗 Merging STIX files...")
        stix.merge_all_stix_files()

        print("\n🕵️ Extracting APT groups...")
        stix.extract_all_apts()

        print("\n⚙️ Generating abilities...")
        ability.generate_abilities_from_matrix()

        print("\n⚙️ Updating abilities with Atomic Red Team commands...")
        ability.translate_all_caldera_abilities()

        print("\n🎯 Generating adversaries...")
        campaign.generate_campaigns()
        intrusionSet.generate_adversaries()

        print("\n⬆️ *** End of Stage 1")
        print("\n⬆️ *** Stage 2")
        # check if there is a key available
        if not config.AZURE_SECRET_KEY:
          print("⚠️  Not using AZURE_SECRET_KEY (value is empty)")
          print("⚠️  Using cache instead !!!")
        else:
         print("✅ AZURE_SECRET_KEY found, running sticks campaign...")
         os.system("python lib/run_sticks.py apt41_dust --full")
         os.system("python lib/run_sticks.py c0010 --full")
         os.system("python lib/run_sticks.py c0026 --full")
         os.system("python lib/run_sticks.py costaricto --full")
         os.system("python lib/run_sticks.py operation_midnighteclipse --full")
         os.system("python lib/run_sticks.py outer_space --full")
         os.system("python lib/run_sticks.py salesforce_data_exfiltration --full")
         os.system("python lib/run_sticks.py shadowray --full")
        print("\n *** End of Stage 2")
        print("\n *** At this point we should run:")
        print("\n python tools/convert_dag_to_caldera.py data/dag/<campaign>_dag.json")
        print("\n ********************************************************")
        print("\n This is where the human-in-the-loop intervention occurs.")
        print("\n ********************************************************")
        print("\n However, this will overwrite the manually curated files and cause an error during execution.")
        print("\n Using STICKS files instead.")
        print("⚠️  Press ANY key to continue ...")
        sys.stdin.read(1)
        print("\n⬆️ *** Stage 3")
        # make sure that caldera is empty to make visualization better for user
        print("⚠️  Empting Caldera ...")
        subprocess.run(["python", "tools/empty_caldera.py"])
        print("⚠️  Loading adversaries and curated abilities ...")
        # load all files that was already manually checked on caldera
        for f in os.listdir("data/api"):
            if f.endswith('.json'):
              if 'adversary' in f.lower():
                 subprocess.run(["python", "tools/load_adversary.py", f"data/api/{f}"])
              elif 'ability' in f.lower():
                 subprocess.run(["python", "tools/load_ability.py", f"data/api/{f}"]) 
                 

        print("⚠️  Running all adversaries in Caldera ...")
        subprocess.run(["python", "tools/load_all_operations.py"]) 
        print("\n Open http://localhost:8888 in your browser, log in with the credentials red/admin, and inspect the currently running operations.")
        print("⚠️  Press ANY key to continue ...")
        sys.stdin.read(1)
        print("\n✅ Pipeline completed successfully!")

    elif args.step == "test":
        print("\n🧪 Uploading abilities (test mode)...")
        print("\n🧪 Chosse something to debug and call it here...")

    elif args.step == "clean":
        os.system('rm data/caldera_adversaries/*') 
        os.system('rm data/caldera_abilities/*') 

    elif args.step == "help":
        print("Usage:")
        print("  python main.py init   # Run full pipeline")
        print("  python main.py test   # Run test mode")
        print("  python main.py help   # Show this help")


if __name__ == "__main__":
    main()
