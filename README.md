# PUBG NameHash Generator

Automated reflection property hash resolution and generation tool for [CUE4Parse](https://github.com/FabianFG/CUE4Parse) and [FModel](https://fmodel.app/).

PUBG periodically applies name obfuscation across reflection properties in engine and game structs (e.g. converting `RowStruct` to hashes like `*8148d9ac28`). This tool compares SDK header dumps, standard Unreal Engine 4 reflection schemas, and historical version baselines to generate an updated `PUBGNameHashMap.json` ready for CUE4Parse Pull Requests.

---

## Features

- **Standard UE4 Engine Resolver**: Automatically resolves obfuscated properties in Unreal Engine classes (`UDataTable`, `AActor`, `UPrimitiveComponent`, etc.).
- **Multi-SDK Structural Diffing**: Tracks property offsets, sizes, types, and bitfields across consecutive game patches.
- **Obfuscated Hierarchy Pairing**: Links obfuscated super-structs (e.g. `F_bd1601cf88` <-> `F_8dac437b7a`) to resolve internal member properties.
- **Input Asset Ingestion**: Automatically scans exported JSON asset files (e.g. WeaponGunData, SkinItemTable, Blueprints, etc.) dropped into `input/` and resolves all their hashes.
- **Multi-Version Chain Lineage**: Traces properties across versions (`2606` -> `2607` -> `2608` -> future) to retain community-verified names.
- **Dual Outputs**:
  - `PUBGNameHashMap.json`: Comprehensive multi-version database ready to drop directly into CUE4Parse.
  - `PUBGNameHashMap_latest.json`: Pruned mapping containing only active hashes for the latest game patch.

---

## Quick Start

### Prerequisites
- Python 3.8+ (no external dependencies required; optional `zstandard` for compressed USMAP files).

### Running the Generator

- **Double-click** `run.bat`, or
- Run via terminal:
  ```bash
  python generate.py
  ```

Generated files will be saved to the `output/` directory:
- `output/PUBGNameHashMap.json` — Comprehensive multi-version map (copy this file into `CUE4Parse/Resources/PUBGNameHashMap.json`).
- `output/PUBGNameHashMap_latest.json` — Latest-version specific active map.
- `output/version_lineage.json` — Detailed evolution matrix of all property transitions.
- `output/summary_report.txt` — Summary statistics report.

---

## When PUBG Updates

1. Dump the new SDK and place the folder in `PUBG SDK/` (e.g. `PUBG SDK 2608.1.1.67`).
2. Open `config.json` and update paths:
   ```json
   {
     "latest_sdk_path": "PUBG SDK/PUBG SDK NEW.x.x.x",
     "previous_sdk_path": "PUBG SDK/PUBG SDK LAST.y.y.y",
     "base_hashmap_paths": [
       "data/base_hashmap_2606.json",
       "data/base_hashmap_2607.json"
     ],
     "ue4_reference_path": "data/ue4_reference.json",
     "custom_overrides_path": "data/custom_overrides.json",
     "usmap_path": "data/usmap/TslGame_04_07_2026.usmap",
     "input_dir": "input",
     "output_dir": "output"
   }
   ```
3. Run `run.bat` or `python generate.py`.

---

## Project Structure

```
PUBG NameHash Generator/
├── config.json                 # Configuration paths
├── generate.py                 # CLI entrypoint
├── run.bat                     # One-click Windows runner
├── requirements.txt            # Dependencies
├── README.md                   # Documentation
├── data/
│   ├── base_hashmap_2606.json  # v42.1 (2606) baseline map
│   ├── base_hashmap_2607.json  # v42.2 (2607) reference map
│   ├── ue4_reference.json      # Standard UE4 reflection properties database
│   ├── custom_overrides.json   # Verified game-specific property overrides
│   └── usmap/                  # Reference USMAP binary dumps
├── src/
│   ├── __init__.py
│   ├── header_parser.py        # Fast C++ SDK header parser
│   ├── usmap_parser.py         # Binary USMAP parser (v1-v4)
│   ├── ue_resolver.py          # UE4 engine reflection resolver
│   ├── struct_differ.py        # Multi-SDK structural diffing & alignment engine
│   └── generator.py            # Pipeline orchestrator & exporter
├── input/                      # Drop exported JSON asset files here to resolve
├── PUBG SDK/                   # Dropped SDK header dumps
└── output/                     # Generated JSONs & reports
```

---

## License

This project is licensed under the [MIT License](LICENSE).
