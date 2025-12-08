import sys
import os
import argparse
from pathlib import Path

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

        print("\n⬆️ Uploading abilities...")
        ability.upload_all_abilities()

        print("\n🎯 Generating adversaries...")
        campaign.generate_campaigns()
        intrusionSet.generate_adversaries()

        print("\n⬆️ Uploading adversaries...")
        campaign.upload_all_campaigns()
        intrusionSet.upload_all_adversaries()

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
