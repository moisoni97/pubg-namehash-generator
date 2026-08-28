import json
import os
import re
from typing import Dict, List, Set, Any, Tuple, Optional
from .header_parser import StructInfo, PropertyInfo
from .usmap_parser import UsmapStruct
from .ue_resolver import UEResolver


class InputAssetScanner:
    HASH_PATTERN = re.compile(r"\*[0-9a-fA-F]{10}")
    HEX_HASH_CLEAN = re.compile(r"^[A-Za-z]?[0-9a-fA-F]{10}$")

    # Map of known Unreal physical surface types in order (EPhysicalSurface)
    SURFACE_NAMES = [
        "Default", "Concrete", "Dirt", "Water", "Metal", "Wood", "Grass",
        "Glass", "Flesh", "Rock", "Sand", "Cloth", "Ice", "Snow",
        "Mud", "Asphalt", "Carpet", "Rubber", "Plaster", "Marble",
        "Gravel", "Hay", "Leaf", "Paper", "Plastic", "Ceramic", "Brick",
        "Cactus", "CamoNet", "Chainlink",
    ]

    STRUCTURAL_KEYS = {
        "objectname", "objectpath", "class", "type", "package", "properties",
        "rows", "outer", "flags", "super", "superstruct", "template", "name",
    }

    def __init__(
        self,
        input_dir: str,
        latest_structs: Dict[str, StructInfo],
        usmap_structs: Optional[Dict[str, UsmapStruct]] = None,
        previous_structs: Optional[Dict[str, StructInfo]] = None,
    ):
        self.input_dir = input_dir
        self.latest_structs = latest_structs
        self.usmap_structs = usmap_structs or {}
        self.previous_structs = previous_structs or {}

        self.scanned_files: List[str] = []
        self.found_hashes: Set[str] = set()
        self.resolved_mappings: Dict[str, str] = {}
        self.parsed_json_trees: List[Tuple[str, Any]] = []
        self.sdk_class_map: Dict[str, str] = {}
        self._hash_to_sdk_locations: Dict[str, List[Tuple[StructInfo, PropertyInfo, int]]] = {}
        self._struct_by_raw: Dict[str, StructInfo] = {}

        self.ue_resolver = UEResolver()

    def scan_and_resolve(self, existing_mappings: Dict[str, str]) -> Tuple[Dict[str, str], int, int]:
        if not self.input_dir or not os.path.exists(self.input_dir):
            return {}, 0, 0

        json_files = []
        for root, _, files in os.walk(self.input_dir):
            for file in files:
                if file.endswith(".json"):
                    json_files.append(os.path.join(root, file))

        self.scanned_files = json_files
        if not json_files:
            return {}, 0, 0

        self.existing_mappings = existing_mappings or {}
        self.resolved_mappings.update(self.existing_mappings)

        self._build_sdk_indices()

        for jf in json_files:
            self._load_and_extract_json(jf)

        # Multi-pass resolution
        max_passes = 8
        for pass_num in range(1, max_passes + 1):
            initial_count = len(self.resolved_mappings)

            for file_path, tree in self.parsed_json_trees:
                self._resolve_from_json_hierarchy(tree)

            for file_path, tree in self.parsed_json_trees:
                self._resolve_structural_keys(tree)

            for file_path, tree in self.parsed_json_trees:
                self._resolve_value_hashes(tree)

            for file_path, tree in self.parsed_json_trees:
                self._resolve_enum_values_from_json(tree)

            for file_path, tree in self.parsed_json_trees:
                self._resolve_from_asset_paths(tree)

            for h in sorted(self.found_hashes):
                if h in self.resolved_mappings:
                    continue
                resolved = self._resolve_hash_from_sdk(h)
                if resolved and not self.HEX_HASH_CLEAN.match(resolved):
                    self.resolved_mappings[h] = resolved

            if len(self.resolved_mappings) == initial_count:
                break

        # Fallbacks for unmapped hashes
        for h in sorted(self.found_hashes):
            if h not in self.resolved_mappings:
                fallback_name = self._generate_universal_fallback(h)
                if fallback_name and not self.HEX_HASH_CLEAN.match(fallback_name):
                    self.resolved_mappings[h] = fallback_name

        final_resolved = {}
        for k, v in self.resolved_mappings.items():
            if k not in self.existing_mappings or self.existing_mappings[k] != v:
                clean_v = v.strip()
                if clean_v and not clean_v.startswith("*") and not clean_v.startswith("_"):
                    if not self.HEX_HASH_CLEAN.match(clean_v) and clean_v.lower() not in self.STRUCTURAL_KEYS:
                        final_resolved[k] = clean_v

        return final_resolved, len(self.scanned_files), len(self.found_hashes)

    def _build_sdk_indices(self):
        for s_name, struct_info in self.latest_structs.items():
            self._struct_by_raw[struct_info.raw_name] = struct_info

            for i, p in enumerate(struct_info.props):
                if p.name.startswith("_") and len(p.name) == 11:
                    h_key = "*" + p.name[1:]
                    if h_key not in self._hash_to_sdk_locations:
                        self._hash_to_sdk_locations[h_key] = []
                    self._hash_to_sdk_locations[h_key].append((struct_info, p, i))

                m_type = re.search(r'[AU]_([0-9a-fA-F]{10})', p.type)
                if m_type:
                    h_type = "*" + m_type.group(1)
                    prop_clean = p.name.lstrip("_")
                    if prop_clean and not prop_clean.startswith("*") and len(prop_clean) > 3:
                        if h_type not in self.sdk_class_map:
                            clean_name = prop_clean
                            for prefix in ("m_", "b_", "p_"):
                                if clean_name.lower().startswith(prefix):
                                    clean_name = clean_name[2:]
                            if clean_name and not self.HEX_HASH_CLEAN.match(clean_name):
                                self.sdk_class_map[h_type] = clean_name[0].upper() + clean_name[1:]

    def _load_and_extract_json(self, file_path: str):
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            for m in self.HASH_PATTERN.finditer(content):
                self.found_hashes.add(m.group(0))
            data = json.loads(content)
            self.parsed_json_trees.append((file_path, data))
        except Exception:
            pass

    def _resolve_from_json_hierarchy(self, data: Any):
        if isinstance(data, list):
            for item in data:
                self._resolve_from_json_hierarchy(item)
        elif isinstance(data, dict):
            obj_type = data.get("Type")
            obj_class = data.get("Class")
            obj_name = data.get("Name")
            obj_path = data.get("ObjectPath")
            super_struct = data.get("SuperStruct")

            if obj_type and obj_name:
                m_type_hash = self.HASH_PATTERN.search(str(obj_type))
                if m_type_hash and not self.HASH_PATTERN.search(str(obj_name)):
                    clean_name = str(obj_name).strip()
                    if clean_name and not self.HEX_HASH_CLEAN.match(clean_name):
                        self.resolved_mappings[m_type_hash.group(0)] = clean_name
                        self.sdk_class_map[m_type_hash.group(0)] = clean_name

            if super_struct and isinstance(super_struct, dict):
                super_obj_name = super_struct.get("ObjectName", "")
                m_super_hash = self.HASH_PATTERN.search(str(super_obj_name))
                if m_super_hash and obj_name:
                    clean_super = str(obj_name)
                    for sfx in ("_Default_C", "_Default", "_C", "_BP_C"):
                        if clean_super.endswith(sfx):
                            clean_super = clean_super[:-len(sfx)]
                            break
                    for pfx in ("BP_", "Default__"):
                        if clean_super.startswith(pfx):
                            clean_super = clean_super[len(pfx):]
                            break
                    if clean_super and not self.HEX_HASH_CLEAN.match(clean_super):
                        self.resolved_mappings[m_super_hash.group(0)] = clean_super
                        self.sdk_class_map[m_super_hash.group(0)] = clean_super

            if obj_name and obj_path:
                m_hash = self.HASH_PATTERN.search(str(obj_name))
                if m_hash and not self.HASH_PATTERN.search(str(obj_path)):
                    clean_target = str(obj_path).split(".")[-1].split("/")[-1].strip()
                    if clean_target and not self.HEX_HASH_CLEAN.match(clean_target):
                        self.resolved_mappings[m_hash.group(0)] = clean_target

            if obj_class:
                m_hash = self.HASH_PATTERN.search(str(obj_class))
                if m_hash:
                    cls_h = m_hash.group(0)
                    if obj_type and isinstance(obj_type, str) and not obj_type.startswith("*") and not self.HEX_HASH_CLEAN.match(obj_type):
                        self.resolved_mappings[cls_h] = obj_type.strip()
                        self.sdk_class_map[cls_h] = obj_type.strip()
                    elif obj_name and isinstance(obj_name, str):
                        candidate_cls = obj_name.split("_", 1)[1] if "_" in obj_name else obj_name
                        if candidate_cls and not self.HEX_HASH_CLEAN.match(candidate_cls):
                            self.resolved_mappings[cls_h] = candidate_cls
                            self.sdk_class_map[cls_h] = candidate_cls

            if obj_type == "DataTable" and "Properties" in data and isinstance(data["Properties"], dict):
                for k, v in data["Properties"].items():
                    m_hash = self.HASH_PATTERN.search(k)
                    if m_hash:
                        self.resolved_mappings[m_hash.group(0)] = "RowStruct"

            for v in data.values():
                self._resolve_from_json_hierarchy(v)

    def _resolve_structural_keys(self, data: Any):
        if isinstance(data, list):
            for item in data:
                self._resolve_structural_keys(item)
        elif isinstance(data, dict):
            keys = set(data.keys())

            if {"Pitch", "Yaw", "Roll"}.issubset(keys) or (len(keys & {"Pitch", "Yaw", "Roll"}) >= 2):
                for k, v in data.items():
                    if isinstance(v, dict):
                        if "Frequency" in v:
                            for sub_k, sub_v in v.items():
                                m_sub_h = self.HASH_PATTERN.search(sub_k)
                                if m_sub_h:
                                    prop_h = m_sub_h.group(0)
                                    if isinstance(sub_v, (int, float)):
                                        self.resolved_mappings[prop_h] = "Amplitude"
                                    else:
                                        self.resolved_mappings[prop_h] = "InitialOffset"
                                        m_val = self.HASH_PATTERN.search(str(sub_v))
                                        if m_val:
                                            self.resolved_mappings[m_val.group(0)] = "EOscillatorWaveform"

            if {"X", "Y", "Z"}.issubset(keys):
                for k, v in data.items():
                    if isinstance(v, dict):
                        if "Frequency" in v:
                            for sub_k, sub_v in v.items():
                                m_sub_h = self.HASH_PATTERN.search(sub_k)
                                if m_sub_h:
                                    prop_h = m_sub_h.group(0)
                                    if isinstance(sub_v, (int, float)):
                                        self.resolved_mappings[prop_h] = "Amplitude"
                                    else:
                                        self.resolved_mappings[prop_h] = "InitialOffset"
                                        m_val = self.HASH_PATTERN.search(str(sub_v))
                                        if m_val:
                                            self.resolved_mappings[m_val.group(0)] = "EOscillatorWaveform"

            for k, v in data.items():
                m_h = self.HASH_PATTERN.search(k)
                if m_h and isinstance(v, dict):
                    v_keys = set(v.keys())
                    if {"Pitch", "Yaw", "Roll"}.issubset(v_keys) or len(v_keys & {"Pitch", "Yaw", "Roll"}) >= 2:
                        self.resolved_mappings[m_h.group(0)] = "RotOscillation"
                    elif {"X", "Y", "Z"}.issubset(v_keys):
                        self.resolved_mappings[m_h.group(0)] = "LocOscillation"

            for v in data.values():
                self._resolve_structural_keys(v)

    def _resolve_value_hashes(self, data: Any, parent_key: Optional[str] = None):
        if isinstance(data, list):
            for item in data:
                self._resolve_value_hashes(item, parent_key)
        elif isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str):
                    m_val_hash = self.HASH_PATTERN.search(v)
                    if m_val_hash:
                        val_h = m_val_hash.group(0)

                        effective_key = None
                        if not k.startswith("*"):
                            effective_key = k
                        elif k in self.resolved_mappings:
                            effective_key = self.resolved_mappings[k]
                        elif parent_key and not parent_key.startswith("*"):
                            effective_key = parent_key

                        if effective_key and effective_key.lower() not in self.STRUCTURAL_KEYS:
                            if val_h not in self.resolved_mappings:
                                if effective_key == "BrushType":
                                    self.resolved_mappings[val_h] = "EBrushType"
                                elif effective_key == "CanCharacterStepUpOn":
                                    self.resolved_mappings[val_h] = "ECanBeCharacterBase"
                                elif effective_key in ("InitialOffset", "Waveform"):
                                    self.resolved_mappings[val_h] = "EOscillatorWaveform"
                                elif effective_key.endswith("Type") or effective_key.endswith("Mode"):
                                    self.resolved_mappings[val_h] = "E" + effective_key
                                else:
                                    self.resolved_mappings[val_h] = effective_key
                self._resolve_value_hashes(v, k if not k.startswith("*") else self.resolved_mappings.get(k, parent_key))

    def _resolve_from_asset_paths(self, data: Any, parent_key: Optional[str] = None):
        if isinstance(data, list):
            for item in data:
                self._resolve_from_asset_paths(item, parent_key)
        elif isinstance(data, dict):
            obj_name = data.get("ObjectName", "")
            obj_path = data.get("ObjectPath", "")
            asset_path_name = data.get("AssetPathName", "")
            sub_path_string = data.get("SubPathString", "")

            # Inline class hash: ObjectName: "*hash'AssetName'"
            m_inline_cls = re.search(r"(\*[0-9a-fA-F]{10})'([^']+)'", str(obj_name))
            if m_inline_cls:
                cls_h = m_inline_cls.group(1)
                asset_name = m_inline_cls.group(2)
                inferred_cls = self._derive_class_from_path(str(obj_path), asset_name)
                if inferred_cls and not self.HEX_HASH_CLEAN.match(inferred_cls):
                    if cls_h not in self.resolved_mappings:
                        self.resolved_mappings[cls_h] = inferred_cls
                        self.sdk_class_map[cls_h] = inferred_cls
                    if parent_key and parent_key.startswith("*") and parent_key not in self.resolved_mappings:
                        self.resolved_mappings[parent_key] = inferred_cls if inferred_cls.endswith("s") else inferred_cls + "s"

            # SoftObject references
            if parent_key and parent_key.startswith("*") and (asset_path_name or sub_path_string):
                target_str = str(sub_path_string or asset_path_name)
                if "StringTables" in target_str or "ST_" in target_str:
                    self.resolved_mappings[parent_key] = "StringTables"
                else:
                    clean_ref = target_str.split("/")[-1].split(".")[-1]
                    if "_" in clean_ref:
                        inferred_prop = clean_ref.split("_", 1)[1]
                        if inferred_prop and not self.HEX_HASH_CLEAN.match(inferred_prop):
                            self.resolved_mappings[parent_key] = inferred_prop

            for k, v in data.items():
                m_prop_hash = self.HASH_PATTERN.search(k)
                current_key = k if not k.startswith("*") else parent_key

                if isinstance(v, dict):
                    v_name = v.get("ObjectName", "")
                    v_path = v.get("ObjectPath", "")
                    v_asset = v.get("AssetPathName", "")
                    v_sub = v.get("SubPathString", "")

                    if m_prop_hash and (v_asset or v_sub):
                        target_str = str(v_sub or v_asset)
                        if "StringTables" in target_str or "ST_" in target_str:
                            self.resolved_mappings[m_prop_hash.group(0)] = "StringTables"
                        else:
                            clean_ref = target_str.split("/")[-1].split(".")[-1]
                            if "_" in clean_ref:
                                inferred_prop = clean_ref.split("_", 1)[1]
                                if inferred_prop and not self.HEX_HASH_CLEAN.match(inferred_prop):
                                    self.resolved_mappings[m_prop_hash.group(0)] = inferred_prop

                    m_v_inline = re.search(r"(\*[0-9a-fA-F]{10})'([^']+)'", str(v_name))
                    if m_v_inline:
                        cls_h = m_v_inline.group(1)
                        asset_name = m_v_inline.group(2)
                        inferred_cls = self._derive_class_from_path(str(v_path), asset_name)
                        if inferred_cls and not self.HEX_HASH_CLEAN.match(inferred_cls):
                            if cls_h not in self.resolved_mappings:
                                self.resolved_mappings[cls_h] = inferred_cls
                                self.sdk_class_map[cls_h] = inferred_cls
                            if m_prop_hash and m_prop_hash.group(0) not in self.resolved_mappings:
                                self.resolved_mappings[m_prop_hash.group(0)] = inferred_cls

                    if m_prop_hash and (v_path or v_name):
                        prop_h = m_prop_hash.group(0)
                        if prop_h not in self.resolved_mappings:
                            inferred_prop = self._derive_property_from_target(str(v_path), str(v_name))
                            if inferred_prop and not self.HEX_HASH_CLEAN.match(inferred_prop):
                                self.resolved_mappings[prop_h] = inferred_prop

                self._resolve_from_asset_paths(v, k if k.startswith("*") else current_key)

    def _derive_class_from_path(self, obj_path: str, asset_name: str) -> Optional[str]:
        if not obj_path and not asset_name:
            return None
        if asset_name.startswith("DA_"):
            return asset_name[3:]
        if "JukeBox" in asset_name and "Data" in asset_name:
            return "JukeBoxData"
        if asset_name.startswith("C_") or "Curve" in obj_path:
            return "CurveFloat"
        if asset_name.startswith("P_") or "Particle" in obj_path:
            return "ParticleSystem"
        if asset_name.startswith("MI_") or asset_name.startswith("M_") or "Material" in obj_path:
            return "Material"
        if asset_name.startswith("T_") or "Texture" in obj_path:
            return "Texture2D"
        if asset_name.startswith("SM_") or "StaticMesh" in obj_path:
            return "StaticMesh"
        if asset_name.startswith("SK_") or "SkeletalMesh" in obj_path:
            return "SkeletalMesh"
        if "Bank" in asset_name or "Bank" in obj_path:
            return "AkAudioBank"
        if "StringTable" in obj_path or "StringTables" in obj_path or asset_name.startswith("ST_"):
            return "StringTable"
        if asset_name.startswith("A_") or "Sound" in obj_path or "Audio" in obj_path:
            return "AkAudioEvent"
        if asset_name.startswith("BP_") or "Blueprint" in obj_path:
            return "BlueprintGeneratedClass"
        parts = [p for p in obj_path.split("/") if p]
        if len(parts) >= 2:
            folder = parts[-2]
            clean = "".join(w.capitalize() for w in folder.replace("_", " ").replace("-", " ").split())
            if clean.endswith("s") and not clean.endswith("ss"):
                clean = clean[:-1]
            if clean and not self.HEX_HASH_CLEAN.match(clean) and clean.lower() not in self.STRUCTURAL_KEYS:
                return clean
        return None

    def _derive_property_from_target(self, obj_path: str, obj_name: str) -> Optional[str]:
        if not obj_path and not obj_name:
            return None
        target_asset = obj_name
        m_name = re.search(r"'(?:.*:)?([^']+)'", str(obj_name))
        if m_name:
            target_asset = m_name.group(1)
        if target_asset.startswith("DA_"):
            return target_asset[3:]
        if "AnimMontage" in str(obj_name) or "Montage" in target_asset:
            return "AnimMontage"
        if "CurveFloat" in str(obj_name) or "Curve" in target_asset:
            return "CurveFloat"
        if "Particle" in str(obj_name) or "Particle" in target_asset:
            return "ParticleSystem"
        if "Material" in str(obj_name) or "Material" in target_asset:
            return "Material"
        if "DamageField" in str(obj_name) or "DamageField" in str(obj_path):
            return "DamageFieldClass"
        if "SCS_Node" in str(obj_name) or "SimpleConstructionScript" in str(obj_path):
            return "RootNodes"
        if "Bank" in target_asset or "Bank" in obj_path:
            return "AudioBanks"
        if "StringTable" in obj_path or "StringTables" in obj_path or target_asset.startswith("ST_"):
            return "StringTables"
        if "AkAudioEvent" in str(obj_name) or "Sound" in str(obj_path) or "Wwise" in str(obj_path):
            clean_sound = target_asset
            for prefix in ("Weapons_Common_", "Weapon_", "_Weapon_", "Weapons_", "UI_", "UI_Common_", "Equip_01_", "UnEquip_01_"):
                if clean_sound.startswith(prefix):
                    clean_sound = clean_sound[len(prefix):]
                    break
            words = [w for w in clean_sound.split("_") if w and not w.isdigit()]
            filtered = [w for w in words if w.lower() not in ("rifle", "steyr", "aug", "a3", "small", "plastic", "01")]
            base = "".join(w.capitalize() for w in (filtered if filtered else words))
            if not base.lower().endswith("sound") and not base.lower().endswith("ak"):
                base += "Sound"
            if base and not self.HEX_HASH_CLEAN.match(base):
                return base
        parts = [p for p in obj_path.split("/") if p]
        if len(parts) >= 2:
            folder = parts[-2]
            clean_name = "".join(w.capitalize() for w in folder.replace("_", " ").replace("-", " ").split())
            if clean_name and not self.HEX_HASH_CLEAN.match(clean_name) and clean_name.lower() not in self.STRUCTURAL_KEYS:
                return clean_name
        return None

    def _resolve_enum_values_from_json(self, data: Any):
        if isinstance(data, list):
            for item in data:
                self._resolve_enum_values_from_json(item)
        elif isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str) and "::" in v:
                    parts = v.split("::")
                    if len(parts) == 2:
                        self._resolve_enum_pair(parts[0], parts[1])
                self._resolve_enum_values_from_json(v)

    def _resolve_enum_pair(self, enum_type_str: str, enum_val_str: str):
        enum_type_hash = self.HASH_PATTERN.search(enum_type_str)
        enum_val_hash = self.HASH_PATTERN.search(enum_val_str)
        if enum_val_hash:
            val_h = enum_val_hash.group(0)
            clean_type_name = None
            if not enum_type_hash and enum_type_str.startswith("E"):
                clean_type_name = enum_type_str
            elif enum_type_hash:
                t_clean = "_" + enum_type_hash.group(0)[1:]
                clean_type_name = self._infer_clean_enum_name_from_headers(t_clean)
                if clean_type_name:
                    self.resolved_mappings[enum_type_hash.group(0)] = clean_type_name
            if clean_type_name:
                val_clean_name = self._infer_enum_value_name(clean_type_name, "_" + val_h[1:])
                if val_clean_name:
                    self.resolved_mappings[val_h] = val_clean_name

    def _infer_clean_enum_name_from_headers(self, enum_hash_name: str) -> Optional[str]:
        for s_name, struct_info in self.latest_structs.items():
            for p in struct_info.props:
                if enum_hash_name in p.type:
                    clean_prop = p.name.lstrip("_")
                    if clean_prop:
                        return "E" + clean_prop[0].upper() + clean_prop[1:]
        return None

    def _infer_enum_value_name(self, enum_name: str, val_hash_clean: str) -> Optional[str]:
        return "Default"

    def _resolve_hash_from_sdk(self, hash_str: str) -> Optional[str]:
        locations = self._hash_to_sdk_locations.get(hash_str, [])
        if not locations:
            h_clean = "_" + hash_str[1:]
            for prefix in ("U", "F", "A"):
                raw = prefix + h_clean
                if raw in self._struct_by_raw:
                    s = self._struct_by_raw[raw]
                    clean = s.clean_name or s.raw_name.lstrip("U").lstrip("F").lstrip("A")
                    if clean and not clean.startswith("_") and not self.HEX_HASH_CLEAN.match(clean):
                        return clean
            return None

        best_result = None
        for struct_info, prop_info, prop_idx in locations:
            result = self._resolve_property_in_context(struct_info, prop_info, prop_idx)
            if result and not self.HEX_HASH_CLEAN.match(result):
                best_result = result

        return best_result

    def _resolve_property_in_context(self, struct_info: StructInfo, prop: PropertyInfo, idx: int) -> Optional[str]:
        s_clean = struct_info.clean_name or struct_info.raw_name.lstrip("U").lstrip("A").lstrip("F")
        if (s_clean, prop.offset) in self.ue_resolver.CORE_ENGINE_MAP:
            return self.ue_resolver.CORE_ENGINE_MAP[(s_clean, prop.offset)]
        if (struct_info.raw_name, prop.offset) in self.ue_resolver.CORE_ENGINE_MAP:
            return self.ue_resolver.CORE_ENGINE_MAP[(struct_info.raw_name, prop.offset)]

        usmap_res = self._align_with_usmap_struct(struct_info, prop)
        if usmap_res:
            return usmap_res

        if "EffectController" in s_clean or "TslEffectController" in s_clean:
            ec_res = self._resolve_effect_controller_prop(prop.offset, prop.type)
            if ec_res:
                return ec_res

        if "CameraShake" in s_clean:
            cs_res = self._resolve_camerashake_prop(prop.offset, prop.type)
            if cs_res:
                return cs_res

        if "8a3bef8952" in struct_info.raw_name or "TickFunction" in struct_info.raw_name or "ActorTick" in struct_info.raw_name:
            if prop.offset == 0x0040 and prop.type == "float":
                return "TickInterval"
            if prop.type == "bool":
                return "bCanEverTick"

        m_cls_h = re.search(r'[AU]_([0-9a-fA-F]{10})', prop.type)
        if m_cls_h:
            cls_key = "*" + m_cls_h.group(1)
            if cls_key in self.sdk_class_map:
                return self.sdk_class_map[cls_key]

        soft_res = self._infer_soft_pointer(prop.type)
        if soft_res:
            return soft_res

        type_res = self._infer_from_cpp_type(prop.type, struct_info.raw_name, prop.offset)
        if type_res:
            return type_res

        sibling_res = self._infer_from_sibling_pattern(struct_info, prop, idx)
        if sibling_res:
            return sibling_res

        neighbor_res = self._infer_from_neighbors(struct_info, prop, idx)
        if neighbor_res:
            return neighbor_res

        if prop.type.startswith("F_"):
            inner_name = self._inspect_inner_struct(prop.type)
            if inner_name:
                return inner_name

            inner_struct = self._struct_by_raw.get(prop.type)
            if inner_struct:
                clean_names = [p.name for p in inner_struct.props if not p.name.startswith("_") or len(p.name) != 11]
                if any("Decal" in n for n in clean_names):
                    return "DecalEffectDataSet"
                if any("Montage" in n or "Anim" in n for n in clean_names):
                    return "AnimDataSet"
                if any("Sound" in n or "Audio" in n for n in clean_names):
                    return "SoundDataSet"

        if struct_info.raw_name.startswith("F_"):
            parent_res = self._infer_struct_member_from_parent_usage(struct_info.raw_name, prop.type)
            if parent_res:
                return parent_res

        if struct_info.raw_name.startswith("F_") and len(struct_info.props) >= 2:
            if struct_info.props[0].offset == 0x0000 and struct_info.props[0].type == "float":
                if struct_info.props[1].offset == 0x0004 and struct_info.props[1].name.lower() == "frequency":
                    if prop.offset == 0x0000:
                        return "Amplitude"
                    if prop.offset == 0x0008:
                        return "InitialOffset"

        return None

    def _resolve_camerashake_prop(self, offset: int, prop_type: str) -> Optional[str]:
        cs_offsets = {
            0x0030: "bSingleInstance",
            0x0034: "OscillationDuration",
            0x0038: "OscillationBlendInTime",
            0x003C: "OscillationBlendOutTime",
            0x0040: "RotOscillation",
            0x0064: "LocOscillation",
            0x0088: "FOVOscillation",
            0x0094: "AnimPlayRate",
            0x0098: "AnimScale",
            0x009C: "AnimBlendInTime",
            0x00A0: "AnimBlendOutTime",
            0x00A4: "RandomAnimSegmentDuration",
            0x00A8: "Anim",
            0x00B0: "bRandomAnimSegment",
            0x00C8: "CameraOwner",
            0x0158: "ShakeScale",
            0x015C: "OscillatorTimeRemaining",
        }
        return cs_offsets.get(offset)

    def _resolve_effect_controller_prop(self, offset: int, prop_type: str) -> Optional[str]:
        ec_offsets = {
            0x0488: "SpawnPoints",
            0x0498: "MinSpawnCount",
            0x049C: "MaxSpawnCount",
            0x04A0: "TotalSpawnLimit",
            0x04A4: "SpawnGroupCount",
            0x04A8: "InnerSegmentCount",
            0x04AC: "MinLimit",
            0x04B0: "SpawnInterval",
            0x04B8: "AttachSocketName",
            0x04C0: "MinSpreadRadius",
            0x04C4: "MaxSpreadRadius",
            0x04C8: "MinSpreadAngle",
            0x04CC: "MaxSpreadAngle",
            0x04D0: "InnerRadius",
            0x04D8: "DamageRadius",
            0x04DC: "OuterRadius",
            0x04E0: "DamageFieldClass",
            0x04E8: "bAttachToSurface",
            0x04EC: "MinLifeTime",
            0x04F0: "MaxLifeTime",
            0x04F4: "FadeInDuration",
            0x04F8: "FadeOutDuration",
            0x04FC: "bAutoDestroy",
            0x0508: "DamageTypeClass",
            0x0528: "InnerSphereComponents",
            0x0538: "MiddleSphereComponents",
            0x0548: "OuterSphereComponents",
            0x0558: "ActiveDamageFields",
            0x0568: "AffectedPawns",
            0x0640: "BaseCollisionComponent",
        }
        return ec_offsets.get(offset)

    def _infer_from_sibling_pattern(self, struct_info: StructInfo, prop: PropertyInfo, idx: int) -> Optional[str]:
        same_type_siblings = []
        clean_siblings = []
        obfuscated_indices = []

        for i, p in enumerate(struct_info.props):
            if p.type == prop.type:
                same_type_siblings.append((i, p))
                if not p.name.startswith("_") or len(p.name) != 11:
                    clean_siblings.append((i, p))
                else:
                    obfuscated_indices.append((i, p))

        if not clean_siblings or len(same_type_siblings) < 2:
            return None

        clean_names = [p.name for _, p in clean_siblings]
        common_suffix = self._find_common_affix(clean_names, suffix=True)
        common_prefix = self._find_common_affix(clean_names, suffix=False)

        our_abs_idx = -1
        for abs_idx, (i, p) in enumerate(same_type_siblings):
            if p.name == prop.name:
                our_abs_idx = abs_idx
                break

        if common_suffix and len(common_suffix) >= 3:
            if common_suffix.lower() == "decal":
                if our_abs_idx >= 0 and our_abs_idx < len(self.SURFACE_NAMES):
                    return self.SURFACE_NAMES[our_abs_idx] + common_suffix
                return f"Surface{our_abs_idx}{common_suffix}"
            return f"{common_suffix}_{our_abs_idx}" if our_abs_idx >= 0 else common_suffix

        if common_prefix and len(common_prefix) >= 3:
            return f"{common_prefix}_{our_abs_idx}" if our_abs_idx >= 0 else common_prefix

        return None

    def _find_common_affix(self, names: List[str], suffix: bool = True) -> str:
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        if suffix:
            reversed_names = [n[::-1] for n in names]
            common = self._common_prefix_of_list(reversed_names)
            return common[::-1]
        else:
            return self._common_prefix_of_list(names)

    @staticmethod
    def _common_prefix_of_list(strings: List[str]) -> str:
        if not strings:
            return ""
        prefix = strings[0]
        for s in strings[1:]:
            while not s.startswith(prefix):
                prefix = prefix[:-1]
                if not prefix:
                    return ""
        return prefix

    def _infer_from_neighbors(self, struct_info: StructInfo, prop: PropertyInfo, idx: int) -> Optional[str]:
        props = struct_info.props
        if idx > 0:
            prev = props[idx - 1]
            prev_clean = prev.name if not prev.name.startswith("_") or len(prev.name) != 11 else None
            if prev_clean:
                if prev_clean.lower() == "health" and prop.type == "float":
                    return "MaxHealth"
                if prev_clean.lower() == "damage" and prop.type == "float":
                    return "DamageRadius"
                if "radius" in prev_clean.lower() and prop.type == "float":
                    return "DamageRadius"

        if idx < len(props) - 1:
            nxt = props[idx + 1]
            nxt_clean = nxt.name if not nxt.name.startswith("_") or len(nxt.name) != 11 else None
            if nxt_clean:
                if nxt_clean.lower() == "health" and prop.type == "float":
                    return "MinHealth"

        return None

    def _inspect_inner_struct(self, struct_type: str) -> Optional[str]:
        raw = struct_type.replace("*", "").strip()
        s = self._struct_by_raw.get(raw)
        if s:
            props = s.props
            if any("Montage" in p.type or "Anim" in p.type for p in props):
                return "AnimConfig"
            if any("Particle" in p.type for p in props):
                return "ParticleConfig"
            if any("Audio" in p.type or "Sound" in p.type for p in props):
                return "SoundConfig"
            if any("Curve" in p.type for p in props):
                return "RecoilConfig"
            if any("PhysicalSurface" in p.type for p in props):
                return "SurfaceModifierMap"
            prop_names = {p.name for p in props}
            if "Material" in prop_names and "Size" in prop_names:
                return "DecalConfig"
            if "Pitch" in prop_names or "Yaw" in prop_names:
                return "RotatorOscillation"
            if "X" in prop_names and "Y" in prop_names:
                return "VectorOscillation"
        return None

    def _infer_struct_member_from_parent_usage(self, struct_raw_name: str, prop_type: str) -> Optional[str]:
        for s_name, s in self.latest_structs.items():
            for p in s.props:
                if struct_raw_name in p.type:
                    parent_prop_clean = p.name.lstrip("_")
                    if parent_prop_clean and len(parent_prop_clean) > 3 and not parent_prop_clean.startswith("*"):
                        base = parent_prop_clean
                        for sfx in ("Infos_Console", "Infos", "Array", "List", "Map", "Settings", "Data"):
                            if base.endswith(sfx):
                                base = base[:-len(sfx)]
                                break
                        if prop_type == "float":
                            return base if base and not self.HEX_HASH_CLEAN.match(base) else "Ratio"
                        if prop_type.startswith("E") and not prop_type.startswith("TArray"):
                            clean_e = prop_type.lstrip("E")
                            return clean_e if clean_e and not self.HEX_HASH_CLEAN.match(clean_e) else None
                        if prop_type == "bool":
                            return "b" + base if base else None
                        if prop_type.startswith("F_") or prop_type.startswith("U") or prop_type.startswith("A"):
                            return base + "Config" if base else None
        return None

    def _infer_soft_pointer(self, prop_type: str) -> Optional[str]:
        m = re.search(r"TSoftObjectPtr<[AU]?([A-Za-z0-9_]+)>", prop_type)
        if not m:
            m = re.search(r"TSoftClassPtr<[AU]?([A-Za-z0-9_]+)>", prop_type)
        if m:
            inner = m.group(1).lstrip("U").lstrip("A")
            if inner.startswith("_"):
                h = "*" + inner[1:]
                if h in self.sdk_class_map:
                    return self.sdk_class_map[h]
            elif not inner.startswith("_") and not self.HEX_HASH_CLEAN.match(inner):
                return inner
        return None

    def _align_with_usmap_struct(self, struct_info: StructInfo, prop_info: PropertyInfo) -> Optional[str]:
        if not self.usmap_structs:
            return None
        us_struct = self.usmap_structs.get(struct_info.clean_name) or self.usmap_structs.get(
            struct_info.raw_name.lstrip("F").lstrip("U").lstrip("A"))
        if not us_struct:
            return None
        idx_in_sdk = -1
        for i, p in enumerate(struct_info.props):
            if p.name == prop_info.name:
                idx_in_sdk = i
                break
        if 0 <= idx_in_sdk < len(us_struct.props):
            candidate = us_struct.props[idx_in_sdk].name
            if candidate and not candidate.startswith("*") and not candidate.startswith("_"):
                return candidate
        return None

    def _infer_from_cpp_type(self, prop_type: str, parent_struct: str, offset: int) -> Optional[str]:
        if prop_type.startswith("TMap<") and prop_type.endswith(">"):
            inner = prop_type[5:-1]
            depth = 0
            split_idx = -1
            for i, ch in enumerate(inner):
                if ch == "<":
                    depth += 1
                elif ch == ">":
                    depth -= 1
                elif ch == "," and depth == 0:
                    split_idx = i
                    break
            if split_idx != -1:
                k = inner[:split_idx].strip()
                v = inner[split_idx + 1:].strip()
                clean_k = re.sub(r"^[EAUF]|TSoftClassPtr<[AU]?|TSoftObjectPtr<[AU]?|\*|>$", "", k)
                if clean_k.startswith("Tsl"):
                    clean_k = clean_k[3:]
                clean_k = clean_k.replace("_", "")
                if "PhysicalSurface" in clean_k:
                    clean_k = "Surface"
                clean_v = re.sub(r"^[EAUF]|TSoftClassPtr<[AU]?|TSoftObjectPtr<[AU]?|\*|>$", "", v)
                if "Particle" in clean_v or "Particle" in v:
                    clean_v = "ParticleMap"
                elif "Decal" in clean_v:
                    clean_v = "DecalMap"
                elif clean_v.startswith("_") or clean_v == "float":
                    clean_v = "ModifierMap"
                elif not clean_v.endswith("Map"):
                    clean_v += "Map"
                res = f"{clean_k}{clean_v}"
                if res and not self.HEX_HASH_CLEAN.match(res):
                    return res

        if prop_type.startswith("TSet<") and prop_type.endswith(">"):
            inner = prop_type[5:-1].strip()
            elem = re.sub(r"^[EAUF]|TSoftClassPtr<[AU]?|TSoftObjectPtr<[AU]?|\*|>$", "", inner)
            if elem.startswith("Tsl"):
                elem = elem[3:]
            elem = elem.replace("_", "")
            if elem and not elem.startswith("_") and not self.HEX_HASH_CLEAN.match(elem):
                return elem if elem.endswith("s") else elem + "s"

        if prop_type.startswith("TArray<") and prop_type.endswith(">"):
            inner = prop_type[7:-1].strip()
            elem = re.sub(r"^[EAUF]|TSoftClassPtr<[AU]?|TSoftObjectPtr<[AU]?|\*|>$", "", inner)
            if elem.startswith("Tsl"):
                elem = elem[3:]
            elem = elem.replace("_", "")
            if elem == "FName":
                return "AttachmentTags" if "Attachment" in parent_struct else "Tags"
            if elem == "FVector" or elem == "Vector":
                return "SpawnPoints"
            if elem and not elem.startswith("_") and not self.HEX_HASH_CLEAN.match(elem):
                return elem if elem.endswith("s") else elem + "s"
            if inner.startswith("F_") or inner.startswith("F"):
                inner_name = self._inspect_inner_struct(inner)
                if inner_name:
                    return inner_name if inner_name.endswith("s") else inner_name + "s"

        m_subclass = re.search(r"TSubclassOf<[AU]?([A-Za-z0-9_]+)>", prop_type)
        if m_subclass:
            inner = m_subclass.group(1).lstrip("A").lstrip("U")
            if inner.startswith("Tsl"):
                inner = inner[3:]
            if not inner.startswith("_") and not self.HEX_HASH_CLEAN.match(inner):
                return inner if inner.endswith("Class") else inner + "Class"

        m_enum = re.search(r"\bE([A-Za-z0-9_]+)\b", prop_type)
        if m_enum and not prop_type.startswith("TArray") and not prop_type.startswith("TMap") and not prop_type.startswith("TSet"):
            enum_name = m_enum.group(1)
            if not enum_name.startswith("_"):
                clean_e = enum_name[6:] if enum_name.startswith("Weapon") else enum_name
                return clean_e

        m_ptr = re.search(r"\b[AU]([A-Za-z0-9_]+)\*", prop_type)
        if m_ptr:
            ptr_type = m_ptr.group(1)
            if not ptr_type.startswith("_") and not self.HEX_HASH_CLEAN.match(ptr_type):
                if "BlendSpace" in ptr_type:
                    return "BlendSpace"
                if "Montage" in ptr_type:
                    return "AnimMontage"
                if "Particle" in ptr_type:
                    return "ParticleSystem"
                if "Sound" in ptr_type or "Audio" in ptr_type:
                    return "AudioEvent"
                if "Texture" in ptr_type:
                    return "Texture"
                if "Mesh" in ptr_type:
                    return "Mesh"
                if "Material" in ptr_type:
                    return "Material"
                return ptr_type

        if prop_type == "FVector":
            return "Vector"
        if prop_type == "FRotator":
            return "Rotation"
        if prop_type in ("FLinearColor", "FColor"):
            return "Color"
        if prop_type == "FTransform":
            return "Transform"

        return None

    def _generate_universal_fallback(self, hash_str: str) -> Optional[str]:
        h_clean = "_" + hash_str[1:]
        for s_name, s in self.latest_structs.items():
            if s.raw_name in ("U" + h_clean, "F" + h_clean, "A" + h_clean, "E" + h_clean):
                return s_name.lstrip("U").lstrip("F").lstrip("A").lstrip("E")
            for p in s.props:
                if p.name == h_clean:
                    parent_clean = s.clean_name or s.raw_name.lstrip("U").lstrip("F").lstrip("A")
                    if p.type.startswith("bool"):
                        return f"b{parent_clean}Param"
                    if p.type in ("float", "double"):
                        return f"{parent_clean}Value"
                    if p.type.startswith("int") or p.type.startswith("uint"):
                        return f"{parent_clean}Count"
                    if p.type == "FVector":
                        return f"{parent_clean}Offset"
                    if p.type == "FRotator":
                        return f"{parent_clean}Rotation"
                    return f"{parent_clean}Property"

        # Blueprint-level fallback directly from JSON trees
        for file_path, tree in self.parsed_json_trees:
            json_res = self._find_hash_in_json_tree(tree, hash_str)
            if json_res and not self.HEX_HASH_CLEAN.match(json_res):
                return json_res

        return None

    def _find_hash_in_json_tree(self, data: Any, target_hash: str) -> Optional[str]:
        if isinstance(data, list):
            for item in data:
                res = self._find_hash_in_json_tree(item, target_hash)
                if res:
                    return res
        elif isinstance(data, dict):
            obj_name = data.get("Name", "")
            obj_type = data.get("Type", "")
            props = data.get("Properties", {})
            if isinstance(props, dict) and target_hash in props:
                val = props[target_hash]
                clean_owner = obj_name or obj_type
                for sfx in ("_C", "_Default_C", "Default__", "Default_"):
                    clean_owner = clean_owner.replace(sfx, "")
                if isinstance(val, bool):
                    return f"b{clean_owner}Param"
                elif isinstance(val, (int, float)):
                    return f"{clean_owner}Value"
                elif isinstance(val, dict):
                    return f"{clean_owner}Config"
                elif isinstance(val, list):
                    return f"{clean_owner}List"
                return f"{clean_owner}Property"
            for v in data.values():
                res = self._find_hash_in_json_tree(v, target_hash)
                if res:
                    return res
        return None
