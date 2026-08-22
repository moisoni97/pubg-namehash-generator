import json
import os
from typing import Dict, List, Tuple, Optional
from .header_parser import StructInfo, PropertyInfo


class UEResolver:
    CORE_ENGINE_MAP = {
        ("DataTable", 0x0028): "RowStruct",
        ("DataTable", 0x0030): "RowStruct",
        ("UDataTable", 0x0028): "RowStruct",
        ("UDataTable", 0x0030): "RowStruct",
        ("Actor", 0x0058): "CustomTimeDilation",
        ("Actor", 0x00B0): "AttachmentReplication",
        ("AActor", 0x0058): "CustomTimeDilation",
        ("AActor", 0x00B0): "AttachmentReplication",
        ("SceneComponent", 0x0220): "AttachSocketName",
        ("USceneComponent", 0x0220): "AttachSocketName",
    }

    STRUCT_SIGNATURES = {
        "RepAttachment": [
            ("AActor*", "AttachParent", 0x0000),
            ("FVector_NetQuantize100", "LocationOffset", 0x0008),
            ("FVector_NetQuantize100", "RelativeScale3D", 0x0014),
            ("FRotator", "RotationOffset", 0x0020),
            ("FName", "AttachSocket", 0x0030),
            ("USceneComponent*", "AttachComponent", 0x0038),
        ],
        "MaterialFunctionInfo": [
            ("FGuid", "StateId", 0x0000),
            ("UMaterialFunction*", "Function", 0x0010),
        ],
        "ScalarParameterValue": [
            ("FMaterialParameterInfo", "ParameterInfo", 0x0000),
            ("float", "ParameterValue", 0x0010),
            ("FGuid", "ExpressionGUID", 0x0014),
        ],
        "VectorParameterValue": [
            ("FMaterialParameterInfo", "ParameterInfo", 0x0000),
            ("FLinearColor", "ParameterValue", 0x0010),
            ("FGuid", "ExpressionGUID", 0x0020),
        ],
        "TextureParameterValue": [
            ("FMaterialParameterInfo", "ParameterInfo", 0x0000),
            ("UTexture*", "ParameterValue", 0x0010),
            ("FGuid", "ExpressionGUID", 0x0018),
        ],
        "StaticSwitchParameter": [
            ("FName", "ParameterName", 0x0000),
            ("bool", "Value", 0x0008),
            ("bool", "bOverride", 0x0009),
            ("FGuid", "ExpressionGUID", 0x000C),
        ],
        "StaticComponentMaskParameter": [
            ("FName", "ParameterName", 0x0000),
            ("bool", "R", 0x0008),
            ("bool", "G", 0x0009),
            ("bool", "B", 0x000A),
            ("bool", "A", 0x000B),
            ("bool", "bOverride", 0x000C),
            ("FGuid", "ExpressionGUID", 0x0010),
        ],
    }

    def __init__(self, reference_db_path: Optional[str] = None):
        self.reference_db: Dict[str, dict] = {}
        if reference_db_path and os.path.exists(reference_db_path):
            with open(reference_db_path, "r", encoding="utf-8") as f:
                self.reference_db = json.load(f)

    def resolve_engine_structs(self, structs: Dict[str, StructInfo]) -> Dict[str, str]:
        resolved: Dict[str, str] = {}

        for s_name, struct_info in structs.items():
            for prop in struct_info.props:
                if prop.is_obfuscated:
                    key = (s_name, prop.offset)
                    raw_key = (struct_info.raw_name, prop.offset)
                    if key in self.CORE_ENGINE_MAP:
                        resolved[prop.hash_key] = self.CORE_ENGINE_MAP[key]
                    elif raw_key in self.CORE_ENGINE_MAP:
                        resolved[prop.hash_key] = self.CORE_ENGINE_MAP[raw_key]

        for s_name, struct_info in structs.items():
            s_props = struct_info.props
            for sig_name, sig_props in self.STRUCT_SIGNATURES.items():
                if len(s_props) == len(sig_props):
                    match_count = 0
                    for s_p, (sig_t, sig_n, sig_off) in zip(s_props, sig_props):
                        type_compatible = (
                            s_p.type == sig_t
                            or ("*" in sig_t and "*" in s_p.type)
                            or ("Vector" in sig_t and "Vector" in s_p.type)
                            or ("int" in sig_t and "int" in s_p.type)
                        )
                        if s_p.offset == sig_off and type_compatible:
                            match_count += 1

                    if match_count == len(sig_props):
                        for s_p, (sig_t, sig_n, sig_off) in zip(s_props, sig_props):
                            if s_p.is_obfuscated and sig_n and sig_n.strip():
                                resolved[s_p.hash_key] = sig_n.strip()

        for s_name, struct_info in structs.items():
            if s_name not in self.reference_db:
                continue

            ref_struct = self.reference_db[s_name]
            ref_props = ref_struct.get("props", [])
            header_props = struct_info.props

            if len(ref_props) == len(header_props):
                for ref_p, header_p in zip(ref_props, header_props):
                    ref_name = ref_p["name"]
                    if (
                        header_p.is_obfuscated
                        and ref_name
                        and ref_name.strip()
                        and not ref_name.startswith("*")
                        and not ref_name.startswith("_")
                    ):
                        resolved[header_p.hash_key] = ref_name.strip()

        return resolved
