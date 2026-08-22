import difflib
import re
from typing import Dict, List, Tuple, Optional, Any, Set
from .header_parser import StructInfo, PropertyInfo
from .usmap_parser import UsmapStruct, UsmapProperty


class VersionTransition:
    def __init__(
        self,
        prev_hash: Optional[str],
        latest_hash: str,
        struct_name: str,
        prop_offset: int,
        prop_type: str,
        name: Optional[str] = None,
    ):
        self.prev_hash = prev_hash
        self.latest_hash = latest_hash
        self.struct_name = struct_name
        self.prop_offset = prop_offset
        self.prop_type = prop_type
        self.name = name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prev_hash": self.prev_hash,
            "latest_hash": self.latest_hash,
            "struct_name": self.struct_name,
            "prop_offset": self.prop_offset,
            "prop_type": self.prop_type,
            "name": self.name,
        }


class StructDiffer:
    def __init__(
        self,
        prev_structs: Dict[str, StructInfo],
        latest_structs: Dict[str, StructInfo],
        known_mappings: Optional[Dict[str, str]] = None,
    ):
        self.prev_structs = prev_structs
        self.latest_structs = latest_structs
        self.known_mappings = known_mappings or {}
        self.transitions: List[VersionTransition] = []
        self.resolved_mappings: Dict[str, str] = {}
        self.hash_to_hash: Dict[str, str] = {}
        self.struct_pairing: Dict[str, str] = {}

    def run_diff(self) -> Dict[str, str]:
        self._pair_obfuscated_structs()
        all_struct_pairs = self._get_all_struct_pairs()

        for prev_name, latest_name in all_struct_pairs:
            prev_s = self.prev_structs.get(prev_name)
            latest_s = self.latest_structs.get(latest_name)
            if not prev_s or not latest_s:
                continue

            self._diff_struct_pair(prev_s, latest_s)

        return self.resolved_mappings

    def _get_all_struct_pairs(self) -> List[Tuple[str, str]]:
        pairs = []
        common_names = set(self.prev_structs.keys()) & set(self.latest_structs.keys())
        for name in common_names:
            pairs.append((name, name))

        for prev_name, latest_name in self.struct_pairing.items():
            if prev_name != latest_name and (prev_name, latest_name) not in pairs:
                pairs.append((prev_name, latest_name))

        return pairs

    def _pair_obfuscated_structs(self):
        common_names = set(self.prev_structs.keys()) & set(self.latest_structs.keys())
        for child_name in common_names:
            p_child = self.prev_structs[child_name]
            l_child = self.latest_structs[child_name]

            p_super = p_child.clean_super
            l_super = l_child.clean_super

            if p_super and l_super and p_super != l_super:
                if p_super.startswith("_") and l_super.startswith("_"):
                    self.struct_pairing[p_super] = l_super

        for s_name in common_names:
            p_props = self.prev_structs[s_name].props
            l_props = self.latest_structs[s_name].props

            p_map = {(p.offset, p.mask): p for p in p_props}
            for l_prop in l_props:
                key = (l_prop.offset, l_prop.mask)
                if key in p_map:
                    p_prop = p_map[key]
                    p_sub = self._extract_obf_type(p_prop.type)
                    l_sub = self._extract_obf_type(l_prop.type)
                    if p_sub and l_sub and p_sub != l_sub:
                        self.struct_pairing[p_sub] = l_sub

    @staticmethod
    def _extract_obf_type(t: str) -> Optional[str]:
        m = re.search(r'[FUA]?(_[0-9a-fA-F]{10})', t)
        if m:
            return m.group(1)
        return None

    def _diff_struct_pair(self, prev_s: StructInfo, latest_s: StructInfo):
        p_props = prev_s.props
        l_props = latest_s.props

        p_names = [
            self.known_mappings.get(p.hash_key, p.name) if p.is_obfuscated else p.name
            for p in p_props
        ]
        l_names = [p.name for p in l_props]

        p_map = {(p.offset, p.mask): (p, name) for p, name in zip(p_props, p_names)}

        for l_prop in l_props:
            key = (l_prop.offset, l_prop.mask)
            if key in p_map:
                p_prop, clean_name = p_map[key]

                type_match = (
                    p_prop.type == l_prop.type
                    or p_prop.size == l_prop.size
                    or (p_prop.is_bitfield and l_prop.is_bitfield)
                )

                if type_match:
                    if l_prop.is_obfuscated:
                        prev_hash = p_prop.hash_key
                        latest_hash = l_prop.hash_key
                        if prev_hash:
                            self.hash_to_hash[prev_hash] = latest_hash

                        if (
                            clean_name
                            and clean_name.strip()
                            and not clean_name.startswith("*")
                            and not clean_name.startswith("_")
                        ):
                            valid_name = clean_name.strip()
                            self.resolved_mappings[latest_hash] = valid_name
                            self.transitions.append(
                                VersionTransition(
                                    prev_hash=prev_hash,
                                    latest_hash=latest_hash,
                                    struct_name=latest_s.clean_name,
                                    prop_offset=l_prop.offset,
                                    prop_type=l_prop.type,
                                    name=valid_name,
                                )
                            )

        matcher = difflib.SequenceMatcher(None, p_names, l_names)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "replace" and (i2 - i1) == (j2 - j1):
                for p_p, l_p, clean_name in zip(p_props[i1:i2], l_props[j1:j2], p_names[i1:i2]):
                    if (
                        l_p.is_obfuscated
                        and clean_name
                        and clean_name.strip()
                        and not clean_name.startswith("*")
                        and not clean_name.startswith("_")
                    ):
                        valid_name = clean_name.strip()
                        self.resolved_mappings[l_p.hash_key] = valid_name
                        self.transitions.append(
                            VersionTransition(
                                prev_hash=p_p.hash_key,
                                latest_hash=l_p.hash_key,
                                struct_name=latest_s.clean_name,
                                prop_offset=l_p.offset,
                                prop_type=l_p.type,
                                name=valid_name,
                            )
                        )


def diff_usmap_to_sdk(
    usmap_structs: Dict[str, UsmapStruct],
    sdk_structs: Dict[str, StructInfo],
    base_map: Dict[str, str],
) -> Dict[str, str]:
    resolved: Dict[str, str] = {}
    common = set(usmap_structs.keys()) & set(sdk_structs.keys())

    for s_name in common:
        u_s = usmap_structs[s_name]
        h_s = sdk_structs[s_name]

        u_props = u_s.props
        h_props = h_s.props

        u_names = [base_map.get(p.name, p.name) for p in u_props]
        h_names = [p.name for p in h_props]

        if len(u_props) == len(h_props):
            for u_p, h_p in zip(u_props, h_props):
                if h_p.is_obfuscated:
                    clean_name = base_map.get(u_p.name, u_p.name)
                    if (
                        clean_name
                        and clean_name.strip()
                        and not clean_name.startswith("*")
                        and not clean_name.startswith("_")
                    ):
                        resolved[h_p.hash_key] = clean_name.strip()

        matcher = difflib.SequenceMatcher(None, u_names, h_names)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "replace" and (i2 - i1) == (j2 - j1):
                for u_p, h_p in zip(u_props[i1:i2], h_props[j1:j2]):
                    if h_p.is_obfuscated:
                        clean_name = base_map.get(u_p.name, u_p.name)
                        if (
                            clean_name
                            and clean_name.strip()
                            and not clean_name.startswith("*")
                            and not clean_name.startswith("_")
                        ):
                            resolved[h_p.hash_key] = clean_name.strip()

    return resolved
