import json
import os
import re
from typing import Dict, List, Tuple, Optional
from .header_parser import StructInfo, PropertyInfo


class UEResolver:
    # Universal UE4 Core Engine Memory Offsets and Names
    CORE_ENGINE_MAP = {
        # DataTable
        ("DataTable", 0x0028): "RowStruct",
        ("DataTable", 0x0030): "RowStruct",
        ("UDataTable", 0x0028): "RowStruct",
        ("UDataTable", 0x0030): "RowStruct",

        # FOscillator
        ("FOscillator", 0x0000): "Amplitude",
        ("FOscillator", 0x0004): "Frequency",
        ("FOscillator", 0x0008): "InitialOffset",
        ("FOscillator", 0x0009): "Waveform",

        # Actor
        ("Actor", 0x0058): "CustomTimeDilation",
        ("Actor", 0x00B0): "AttachmentReplication",
        ("AActor", 0x0058): "CustomTimeDilation",
        ("AActor", 0x00B0): "AttachmentReplication",

        # SceneComponent
        ("SceneComponent", 0x0220): "AttachSocketName",
        ("USceneComponent", 0x0220): "AttachSocketName",

        # SimpleConstructionScript & SCS_Node
        ("SCS_Node", 0x0028): "ComponentClass",
        ("SCS_Node", 0x0030): "ComponentTemplate",
        ("USCS_Node", 0x0028): "ComponentClass",
        ("USCS_Node", 0x0030): "ComponentTemplate",

        # Brush & Volume
        ("Volume", 0x0400): "BrushType",
        ("AVolume", 0x0400): "BrushType",
        ("PostProcessVolume", 0x0400): "BrushType",
        ("APostProcessVolume", 0x0400): "BrushType",

        # AkAudio
        ("AkAudioBank", 0x0030): "AutoLoad",
        ("UAkAudioBank", 0x0030): "AutoLoad",

        # ActorTickFunction
        ("ActorTickFunction", 0x0040): "TickInterval",
        ("FActorTickFunction", 0x0040): "TickInterval",
    }

    RELATIVE_ENGINE_MAP = {
        "DataTable": {
            0x0000: ("RowStruct", ("UScriptStruct", "*")),
        },
        "CameraShake": {
            0x0000: ("bSingleInstance", ("bool",)),
            0x0004: ("OscillationDuration", ("float", "double")),
            0x0008: ("OscillationBlendInTime", ("float", "double")),
            0x000C: ("OscillationBlendOutTime", ("float", "double")),
            0x0010: ("RotOscillation", None),
            0x0034: ("LocOscillation", None),
            0x0058: ("FOVOscillation", None),
        },
        "SCS_Node": {
            0x0000: ("ComponentClass", ("TSubclassOf", "*")),
            0x0008: ("ComponentTemplate", ("*",)),
        },
        "SimpleConstructionScript": {
            0x0000: ("RootNodes", ("TArray",)),
            0x0010: ("AllNodes", ("TArray",)),
            0x0020: ("DefaultSceneRootNode", ("USCS_Node*", "*")),
        },
        "Brush": {
            0x0000: ("BrushType", ("E_", "EBrushType", "uint8_t")),
            0x0004: ("BrushColor", ("FColor",)),
            0x0008: ("PolyFlags", ("int32_t", "uint32_t")),
            0x0010: ("Brush", ("UModel*",)),
            0x0018: ("BrushComponent", ("UBrushComponent*",)),
        },
    }

    STRUCT_SIGNATURES = {
        "RotatorOscillation": [
            ("FOscillator", "Pitch", 0x0000),
            ("FOscillator", "Yaw", 0x000C),
            ("FOscillator", "Roll", 0x0018),
        ],
        "VectorOscillation": [
            ("FOscillator", "X", 0x0000),
            ("FOscillator", "Y", 0x000C),
            ("FOscillator", "Z", 0x0018),
        ],
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
            existing_names = {p.name for p in struct_info.props if not p.is_obfuscated}
            used_names = set(existing_names)
            base_off = struct_info.props[0].offset if struct_info.props else 0
            clean_s = struct_info.clean_name or s_name
            rel_map = self.RELATIVE_ENGINE_MAP.get(clean_s) or self.RELATIVE_ENGINE_MAP.get(s_name)

            for prop in struct_info.props:
                if prop.is_obfuscated:
                    name = None
                    if rel_map is not None:
                        rel_off = prop.offset - base_off
                        if rel_off in rel_map:
                            cand_name, type_Check = rel_map[rel_off]
                            if type_Check is None or any(tc in prop.type for tc in type_Check) or (
                                "bool" in type_Check and prop.is_bitfield
                            ):
                                name = cand_name

                    if not name:
                        key = (s_name, prop.offset)
                        raw_key = (struct_info.raw_name, prop.offset)
                        clean_key = (clean_s, prop.offset)
                        if key in self.CORE_ENGINE_MAP:
                            name = self.CORE_ENGINE_MAP[key]
                        elif raw_key in self.CORE_ENGINE_MAP:
                            name = self.CORE_ENGINE_MAP[raw_key]
                        elif clean_key in self.CORE_ENGINE_MAP:
                            name = self.CORE_ENGINE_MAP[clean_key]

                    if name:
                        if name in used_names:
                            continue
                        if name == "bSingleInstance" and not (prop.type == "bool" or prop.is_bitfield):
                            continue
                        if name == "OscillationDuration" and prop.type not in ("float", "double"):
                            continue
                        if name == "RowStruct" and not ("*" in prop.type or "UScriptStruct" in prop.type):
                            continue
                        used_names.add(name)
                        resolved[prop.hash_key] = name

        # Signature matching for oscillator and parameter structs
        for s_name, struct_info in structs.items():
            s_props = struct_info.props
            # Special check for FOscillator
            if len(s_props) >= 2 and s_props[0].offset == 0x0000 and s_props[0].type == "float":
                if s_props[1].offset == 0x0004 and s_props[1].name.lower() == "frequency":
                    if s_props[0].is_obfuscated:
                        resolved[s_props[0].hash_key] = "Amplitude"
                    if len(s_props) >= 3 and s_props[2].offset == 0x0008 and s_props[2].is_obfuscated:
                        resolved[s_props[2].hash_key] = "InitialOffset"

            for sig_name, sig_props in self.STRUCT_SIGNATURES.items():
                if len(s_props) == len(sig_props):
                    match_count = 0
                    for s_p, (sig_t, sig_n, sig_off) in zip(s_props, sig_props):
                        type_compatible = (
                            s_p.type == sig_t
                            or ("*" in sig_t and "*" in s_p.type)
                            or ("Vector" in sig_t and "Vector" in s_p.type)
                            or ("int" in sig_t and "int" in s_p.type)
                            or (sig_t == "float" and s_p.type in ("float", "double"))
                        )
                        if s_p.offset == sig_off and type_compatible:
                            match_count += 1

                    if match_count == len(sig_props):
                        for s_p, (sig_t, sig_n, sig_off) in zip(s_props, sig_props):
                            if s_p.is_obfuscated:
                                resolved[s_p.hash_key] = sig_n

        return resolved
