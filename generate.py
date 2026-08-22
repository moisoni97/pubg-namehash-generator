#!/usr/bin/env python3
import os
import sys
import json
import argparse

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from src.generator import NameHashGenerator


def main():
    parser = argparse.ArgumentParser(
        description="PUBG NameHash Generator"
    )
    parser.add_argument(
        "-c", "--config",
        default="config.json",
        help="Path to configuration JSON file (default: config.json)",
    )
    parser.add_argument(
        "-l", "--latest",
        help="Override path to latest PUBG SDK dump directory",
    )
    parser.add_argument(
        "-p", "--previous",
        help="Override path to previous PUBG SDK dump directory",
    )
    parser.add_argument(
        "-u", "--usmap",
        help="Override path to reference USMAP file",
    )
    parser.add_argument(
        "-o", "--output",
        help="Override path to output directory",
    )

    args = parser.parse_args()

    config_path = os.path.join(PROJECT_ROOT, args.config) if not os.path.isabs(args.config) else args.config
    if not os.path.exists(config_path):
        print(f"Error: Config file not found at {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if args.latest:
        config["latest_sdk_path"] = args.latest
    if args.previous:
        config["previous_sdk_path"] = args.previous
    if args.usmap:
        config["usmap_path"] = args.usmap
    if args.output:
        config["output_dir"] = args.output

    for k in [
        "latest_sdk_path",
        "previous_sdk_path",
        "base_hashmap_path",
        "ue4_reference_path",
        "custom_overrides_path",
        "usmap_path",
        "output_dir",
    ]:
        if config.get(k) and not os.path.isabs(config[k]):
            config[k] = os.path.normpath(os.path.join(PROJECT_ROOT, config[k]))

    generator = NameHashGenerator(config)
    generator.run()


if __name__ == "__main__":
    main()
