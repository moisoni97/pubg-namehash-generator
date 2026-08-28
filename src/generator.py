import json
import os
import time
from typing import Dict, Optional, Any, List

from .header_parser import parse_sdk, StructInfo
from .usmap_parser import parse_usmap, UsmapStruct
from .ue_resolver import UEResolver
from .struct_differ import StructDiffer, diff_usmap_to_sdk, VersionTransition
from .input_scanner import InputAssetScanner


class NameHashGenerator:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.latest_sdk_path = config.get("latest_sdk_path", "")
        self.previous_sdk_path = config.get("previous_sdk_path", "")

        raw_base_paths = config.get("base_hashmap_paths")
        if isinstance(raw_base_paths, list):
            self.base_hashmap_paths = raw_base_paths
        elif isinstance(raw_base_paths, str):
            self.base_hashmap_paths = [raw_base_paths]
        elif config.get("base_hashmap_path"):
            self.base_hashmap_paths = [config.get("base_hashmap_path")]
        else:
            self.base_hashmap_paths = ["data/base_hashmap_2606.json", "data/base_hashmap_2607.json"]

        self.ue4_reference_path = config.get("ue4_reference_path", "")
        self.custom_overrides_path = config.get("custom_overrides_path", "")
        self.usmap_path = config.get("usmap_path", "")
        self.input_dir = config.get("input_dir", "input")
        self.output_dir = config.get("output_dir", "output")

        self.base_map: Dict[str, str] = {}
        self.custom_overrides: Dict[str, str] = {}
        self.previous_output_map: Dict[str, str] = {}
        self.latest_mappings: Dict[str, str] = {}
        self.all_mappings: Dict[str, str] = {}
        self.lineage_records: List[Dict[str, Any]] = []
        self.input_files_count: int = 0
        self.input_hashes_count: int = 0
        self.input_hashes_resolved: int = 0

    def run(self) -> Dict[str, str]:
        t0 = time.time()
        print("=" * 60)
        print("  PUBG NameHash Generator")
        print("=" * 60)

        os.makedirs(self.output_dir, exist_ok=True)

        self._load_baselines()

        usmap_structs: Dict[str, UsmapStruct] = {}
        if self.usmap_path and os.path.exists(self.usmap_path):
            print(f"\n[1/6] Parsing Reference USMAP: {os.path.basename(self.usmap_path)}...")
            usmap_structs = parse_usmap(self.usmap_path)
            print(f"      Parsed {len(usmap_structs)} reference structs from USMAP.")

        prev_structs: Dict[str, StructInfo] = {}
        prev_resolved: Dict[str, str] = {}
        if self.previous_sdk_path and os.path.exists(self.previous_sdk_path):
            print(f"\n[2/6] Parsing Previous SDK: {os.path.basename(self.previous_sdk_path)}...")
            prev_structs = parse_sdk(self.previous_sdk_path)
            print(f"      Parsed {len(prev_structs)} structs from Previous SDK.")

            if usmap_structs:
                prev_resolved = diff_usmap_to_sdk(usmap_structs, prev_structs, self.base_map)
                print(f"      Resolved {len(prev_resolved)} properties for Previous SDK from USMAP/BaseMap.")

        print(f"\n[3/6] Parsing Latest SDK: {os.path.basename(self.latest_sdk_path)}...")
        latest_structs = parse_sdk(self.latest_sdk_path)
        print(f"      Parsed {len(latest_structs)} structs from Latest SDK.")

        print("\n[4/6] Resolving Standard UE4 Engine Reflection...")
        ue_resolver = UEResolver(self.ue4_reference_path)
        ue_resolved = ue_resolver.resolve_engine_structs(latest_structs)
        print(f"      Resolved {len(ue_resolved)} properties via UE4 Engine Reflection.")

        print("\n[5/6] Performing Multi-SDK Structural Diffing & Lineage Tracking...")
        known_combined = dict(self.base_map)
        known_combined.update(self.previous_output_map)
        known_combined.update(prev_resolved)

        differ = StructDiffer(prev_structs, latest_structs, known_combined)
        diff_resolved = differ.run_diff()
        print(f"      Mapped {len(diff_resolved)} properties through cross-SDK structural alignment.")

        self.latest_mappings = {}
        self.latest_mappings.update(ue_resolved)
        self.latest_mappings.update(diff_resolved)
        self.latest_mappings.update(self.custom_overrides)

        self.all_mappings = {}
        self.all_mappings.update(self.base_map)
        self.all_mappings.update(self.previous_output_map)
        self.all_mappings.update(prev_resolved)
        self.all_mappings.update(self.latest_mappings)

        # 6. Scan and resolve input asset files
        if self.input_dir and os.path.exists(self.input_dir):
            print("\n[6/6] Scanning & Resolving Input Asset JSONs...")
            scanner = InputAssetScanner(self.input_dir, latest_structs, usmap_structs, prev_structs)
            input_resolved, f_count, h_count = scanner.scan_and_resolve(self.all_mappings)
            self.input_files_count = f_count
            self.input_hashes_count = h_count
            self.input_hashes_resolved = len(input_resolved)
            print(f"      Scanned {f_count} file(s), found {h_count} hashes, resolved {len(input_resolved)} new mappings.")
            self.latest_mappings.update(input_resolved)
            self.all_mappings.update(input_resolved)

        self._build_lineage(differ)
        self._export_outputs(t0)

        return self.latest_mappings

    def _load_baselines(self):
        # 1. Load each base hashmap specified in base_hashmap_paths
        for p in self.base_hashmap_paths:
            if p and os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        raw_map = json.load(f)
                    valid_entries = {
                        k: v.strip()
                        for k, v in raw_map.items()
                        if v and v.strip() and not v.startswith("*") and not v.startswith("_")
                    }
                    self.base_map.update(valid_entries)
                    print(f"Loaded baseline {os.path.basename(p)}: {len(valid_entries)} entries.")
                except Exception as e:
                    print(f"Warning: Could not load baseline {p}: {e}")

        if self.custom_overrides_path and os.path.exists(self.custom_overrides_path):
            with open(self.custom_overrides_path, "r", encoding="utf-8") as f:
                raw_overrides = json.load(f)
            self.custom_overrides = {
                k: v.strip()
                for k, v in raw_overrides.items()
                if v and v.strip() and not v.startswith("*") and not v.startswith("_")
            }
            print(f"Loaded custom overrides: {len(self.custom_overrides)} entries.")

        out_all_path = os.path.join(self.output_dir, "PUBGNameHashMap.json")
        if os.path.exists(out_all_path):
            try:
                with open(out_all_path, "r", encoding="utf-8") as f:
                    saved_map = json.load(f)
                self.previous_output_map = {
                    k: v.strip()
                    for k, v in saved_map.items()
                    if v and v.strip() and not v.startswith("*") and not v.startswith("_")
                }
                print(f"Loaded previous output database: {len(self.previous_output_map)} entries.")
            except Exception:
                pass

    def _build_lineage(self, differ: StructDiffer):
        self.lineage_records = []
        for trans in differ.transitions:
            if trans.name and trans.name.strip():
                self.lineage_records.append(trans.to_dict())

    def _export_outputs(self, start_time: float):
        sorted_latest = {
            k: v.strip()
            for k, v in sorted(self.latest_mappings.items())
            if v and v.strip() and not v.startswith("*") and not v.startswith("_")
        }
        sorted_all = {
            k: v.strip()
            for k, v in sorted(self.all_mappings.items())
            if v and v.strip() and not v.startswith("*") and not v.startswith("_")
        }

        # Main CUE4Parse output (comprehensive multi-version map)
        out_all_path = os.path.join(self.output_dir, "PUBGNameHashMap.json")
        with open(out_all_path, "w", encoding="utf-8") as f:
            json.dump(sorted_all, f, indent=4)
        print(f"\n[+] Exported: {out_all_path} ({len(sorted_all)} total mappings)")

        # Latest version specific map
        out_latest_path = os.path.join(self.output_dir, "PUBGNameHashMap_latest.json")
        with open(out_latest_path, "w", encoding="utf-8") as f:
            json.dump(sorted_latest, f, indent=4)
        print(f"[+] Exported: {out_latest_path} ({len(sorted_latest)} latest mappings)")

        # Version lineage record
        out_lineage_path = os.path.join(self.output_dir, "version_lineage.json")
        with open(out_lineage_path, "w", encoding="utf-8") as f:
            json.dump(self.lineage_records, f, indent=2)
        print(f"[+] Exported: {out_lineage_path}")

        # Summary text report
        out_report_path = os.path.join(self.output_dir, "summary_report.txt")
        elapsed = time.time() - start_time
        rowstruct_latest = [k for k, v in sorted_latest.items() if v == "RowStruct"]
        rowstruct_all = [k for k, v in sorted_all.items() if v == "RowStruct"]

        report_lines = [
            "=" * 60,
            "PUBG NameHash Generator - Summary Report",
            "=" * 60,
            f"Execution Time:             {elapsed:.2f} seconds",
            f"Total Multi-Version Hashes: {len(sorted_all)}",
            f"Latest Active Hashes:       {len(sorted_latest)}",
            f"Tracked Lineage Records:    {len(self.lineage_records)}",
            f"Input Files Scanned:        {self.input_files_count}",
            f"Input Hashes Resolved:      {self.input_hashes_resolved}",
            f"RowStruct Hash (Latest):    {rowstruct_latest[0] if rowstruct_latest else 'NOT FOUND'}",
            f"RowStruct Hashes (All):     {', '.join(rowstruct_all) if rowstruct_all else 'NOT FOUND'}",
            "=" * 60,
            "STATUS: COMPLETED SUCCESSFULLY",
            "=" * 60,
        ]

        report_content = "\n".join(report_lines)
        with open(out_report_path, "w", encoding="utf-8") as f:
            f.write(report_content)
        print(f"[+] Exported: {out_report_path}")

        print("\n" + report_content)
