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
        paired_latest: Set[str] = set()

        def add_pair(p_s: str, l_s: str) -> bool:
            if not (p_s and l_s and p_s != l_s and p_s.startswith("_") and l_s.startswith("_")):
                return False
            if p_s in common_names or l_s in common_names:
                return False
            if p_s not in self.prev_structs or l_s not in self.latest_structs:
                return False
            p_info = self.prev_structs[p_s]
            l_info = self.latest_structs[l_s]
            if p_info.raw_name[0] != l_info.raw_name[0] or p_info.module != l_info.module:
                return False
            p_clean = {p.name for p in p_info.props if not p.is_obfuscated}
            l_clean = {p.name for p in l_info.props if not p.is_obfuscated}
            if p_clean and l_clean and not (p_clean & l_clean):
                return False
            if p_s not in self.struct_pairing and l_s not in paired_latest:
                self.struct_pairing[p_s] = l_s
                paired_latest.add(l_s)
                return True
            return False

        def norm_obf_generic(t: str) -> str:
            return re.sub(r"_[0-9a-fA-F]{10}", "_OBF", t)

        # Stage 1: Pair obfuscated structs that share a unique known resolved name
        prev_by_known: Dict[str, List[str]] = {}
        for name, s in self.prev_structs.items():
            if name.startswith("_") and s.hash_key and s.hash_key in self.known_mappings:
                k_name = f"{s.raw_name[0]}:{self.known_mappings[s.hash_key]}"
                prev_by_known.setdefault(k_name, []).append(name)

        latest_by_known: Dict[str, List[str]] = {}
        for name, s in self.latest_structs.items():
            if name.startswith("_") and s.hash_key and s.hash_key in self.known_mappings:
                k_name = f"{s.raw_name[0]}:{self.known_mappings[s.hash_key]}"
                latest_by_known.setdefault(k_name, []).append(name)

        for k_name, p_list in prev_by_known.items():
            l_list = latest_by_known.get(k_name, [])
            if len(p_list) == 1 and len(l_list) == 1:
                add_pair(p_list[0], l_list[0])

        # Stage 2: Exact clean-property signature match (>= 2 clean properties)
        def clean_sig(s: StructInfo):
            c_props = tuple(
                (p.name, norm_obf_generic(p.type))
                for p in s.props
                if not p.is_obfuscated
            )
            if len(c_props) < 2:
                return None
            sup = s.clean_super if (s.clean_super and not s.clean_super.startswith("_")) else ""
            return (s.module, s.raw_name[0], sup, c_props)

        prev_by_sig: Dict[Any, List[str]] = {}
        for name, s in self.prev_structs.items():
            if name.startswith("_"):
                sig = clean_sig(s)
                if sig:
                    prev_by_sig.setdefault(sig, []).append(name)

        latest_by_sig: Dict[Any, List[str]] = {}
        for name, s in self.latest_structs.items():
            if name.startswith("_"):
                sig = clean_sig(s)
                if sig:
                    latest_by_sig.setdefault(sig, []).append(name)

        for sig, p_list in prev_by_sig.items():
            l_list = latest_by_sig.get(sig, [])
            if len(p_list) == 1 and len(l_list) == 1:
                add_pair(p_list[0], l_list[0])

        # Stage 3: High-overlap clean-property Jaccard matching (>= 3 shared clean properties)
        unpaired_prev = [
            n for n in self.prev_structs
            if n.startswith("_") and n not in self.struct_pairing
        ]
        unpaired_latest = [
            n for n in self.latest_structs
            if n.startswith("_") and n not in paired_latest
        ]

        l_clean_sets: Dict[str, Set[Tuple[str, str]]] = {}
        for l_n in unpaired_latest:
            s = self.latest_structs[l_n]
            c_set = {(p.name, norm_obf_generic(p.type)) for p in s.props if not p.is_obfuscated}
            if len(c_set) >= 3:
                l_clean_sets[l_n] = c_set

        candidates: List[Tuple[float, int, str, str]] = []
        for p_n in unpaired_prev:
            p_s = self.prev_structs[p_n]
            p_set = {(p.name, norm_obf_generic(p.type)) for p in p_s.props if not p.is_obfuscated}
            if len(p_set) < 3:
                continue
            for l_n, l_set in l_clean_sets.items():
                l_s = self.latest_structs[l_n]
                if p_s.module != l_s.module or p_s.raw_name[0] != l_s.raw_name[0]:
                    continue
                inter = len(p_set & l_set)
                if inter < 3:
                    continue
                union = len(p_set | l_set)
                score = inter / union
                if score >= 0.6:
                    candidates.append((score, inter, p_n, l_n))

        candidates.sort(reverse=True)
        for score, inter, p_n, l_n in candidates:
            add_pair(p_n, l_n)

        # Stage 4: Iterative transitive propagation via superclasses, offsets, property names, and sequences
        def run_transitive_propagation():
            changed = True
            while changed:
                changed = False
                active_pairs = [(n, n) for n in common_names] + list(self.struct_pairing.items())
                for p_name, l_name in active_pairs:
                    p_s = self.prev_structs.get(p_name)
                    l_s = self.latest_structs.get(l_name)
                    if not p_s or not l_s:
                        continue

                    # 4a. Superclass propagation
                    if p_s.clean_super and l_s.clean_super:
                        if add_pair(p_s.clean_super, l_s.clean_super):
                            changed = True

                    p_props = p_s.props
                    l_props = l_s.props

                    # 4b. Property (offset, mask) match when property index or count also aligns
                    p_off_map = {(p.offset, p.mask): (pi, p) for pi, p in enumerate(p_props)}
                    for li, l_p in enumerate(l_props):
                        key = (l_p.offset, l_p.mask)
                        if key in p_off_map:
                            pi, p_p = p_off_map[key]
                            if pi == li or len(p_props) == len(l_props):
                                p_sub = self._extract_obf_type(p_p.type)
                                l_sub = self._extract_obf_type(l_p.type)
                                if p_sub and l_sub and add_pair(p_sub, l_sub):
                                    changed = True

                    # 4c. Clean or known property name match (unique within struct)
                    p_resolved_names = [
                        self.known_mappings.get(p.hash_key, p.name) if p.is_obfuscated else p.name
                        for p in p_props
                    ]
                    l_resolved_names = [
                        self.known_mappings.get(p.hash_key, p.name) if p.is_obfuscated else p.name
                        for p in l_props
                    ]

                    p_by_name: Dict[str, List[PropertyInfo]] = {}
                    for p, rn in zip(p_props, p_resolved_names):
                        if rn and not rn.startswith("_") and not rn.startswith("*"):
                            p_by_name.setdefault(rn, []).append(p)

                    l_by_name: Dict[str, List[PropertyInfo]] = {}
                    for p, rn in zip(l_props, l_resolved_names):
                        if rn and not rn.startswith("_") and not rn.startswith("*"):
                            l_by_name.setdefault(rn, []).append(p)

                    for rn, p_list in p_by_name.items():
                        l_list = l_by_name.get(rn, [])
                        if len(p_list) == 1 and len(l_list) == 1:
                            p_sub = self._extract_obf_type(p_list[0].type)
                            l_sub = self._extract_obf_type(l_list[0].type)
                            if p_sub and l_sub and add_pair(p_sub, l_sub):
                                changed = True

                    # 4d. SequenceMatcher replace blocks on property names
                    matcher = difflib.SequenceMatcher(None, p_resolved_names, l_resolved_names)
                    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                        if tag == "replace" and (i2 - i1) == (j2 - j1):
                            for p_p, l_p in zip(p_props[i1:i2], l_props[j1:j2]):
                                p_sub = self._extract_obf_type(p_p.type)
                                l_sub = self._extract_obf_type(l_p.type)
                                if p_sub and l_sub and add_pair(p_sub, l_sub):
                                    changed = True

                    # 4e. SequenceMatcher equal blocks on normalized types
                    p_norm_t = [norm_obf_generic(p.type) for p in p_props]
                    l_norm_t = [norm_obf_generic(p.type) for p in l_props]
                    t_matcher = difflib.SequenceMatcher(None, p_norm_t, l_norm_t)
                    for tag, i1, i2, j1, j2 in t_matcher.get_opcodes():
                        if tag == "equal":
                            for pi, li in zip(range(i1, i2), range(j1, j2)):
                                if abs(p_props[pi].offset - l_props[li].offset) <= 256:
                                    p_sub = self._extract_obf_type(p_props[pi].type)
                                    l_sub = self._extract_obf_type(l_props[li].type)
                                    if p_sub and l_sub and add_pair(p_sub, l_sub):
                                        changed = True

        run_transitive_propagation()

        # Stage 5: Unique layout/type-sequence signature for remaining structs with >= 4 properties
        def layout_sig(s: StructInfo, is_latest: bool):
            if len(s.props) < 4:
                return None
            base_off = s.props[0].offset
            sup = s.clean_super or ""
            if not is_latest and sup in self.struct_pairing:
                sup = self.struct_pairing[sup]
            elif sup.startswith("_") and (sup not in paired_latest if is_latest else True):
                sup = "_OBF"
            prop_tuples = tuple(
                (
                    p.offset - base_off,
                    self._normalize_type(p.type, is_latest=is_latest, paired_latest=paired_latest),
                    p.mask,
                )
                for p in s.props
            )
            return (s.module, s.raw_name[0], sup, prop_tuples)

        p_layout: Dict[Any, List[str]] = {}
        for name, s in self.prev_structs.items():
            if name.startswith("_") and name not in self.struct_pairing:
                sig = layout_sig(s, is_latest=False)
                if sig:
                    p_layout.setdefault(sig, []).append(name)

        l_layout: Dict[Any, List[str]] = {}
        for name, s in self.latest_structs.items():
            if name.startswith("_") and name not in paired_latest:
                sig = layout_sig(s, is_latest=True)
                if sig:
                    l_layout.setdefault(sig, []).append(name)

        stage5_added = False
        for sig, p_list in p_layout.items():
            l_list = l_layout.get(sig, [])
            if len(p_list) == 1 and len(l_list) == 1:
                if add_pair(p_list[0], l_list[0]):
                    stage5_added = True

        if stage5_added:
            run_transitive_propagation()

        # Transfer known struct/class hash names across paired obfuscated structs
        for p_s_name, l_s_name in self.struct_pairing.items():
            p_s = self.prev_structs.get(p_s_name)
            l_s = self.latest_structs.get(l_s_name)
            if p_s and l_s and p_s.hash_key and l_s.hash_key:
                self.hash_to_hash[p_s.hash_key] = l_s.hash_key
                known_s_name = self.known_mappings.get(p_s.hash_key)
                if (
                    known_s_name
                    and known_s_name.strip()
                    and not known_s_name.startswith("*")
                    and not known_s_name.startswith("_")
                ):
                    valid_s_name = known_s_name.strip()
                    self.resolved_mappings[l_s.hash_key] = valid_s_name
                    self.transitions.append(
                        VersionTransition(
                            prev_hash=p_s.hash_key,
                            latest_hash=l_s.hash_key,
                            struct_name=l_s.clean_name,
                            prop_offset=0,
                            prop_type=l_s.raw_name,
                            name=valid_s_name,
                        )
                    )

    @staticmethod
    def _extract_obf_type(t: str) -> Optional[str]:
        m = re.search(r"[FUA]?(_[0-9a-fA-F]{10})", t)
        if m:
            return m.group(1)
        return None

    def _normalize_type(
        self,
        t: str,
        is_latest: bool = False,
        paired_latest: Optional[Set[str]] = None,
    ) -> str:
        def repl(m: re.Match) -> str:
            prefix = m.group(1) or ""
            obf = m.group(2)
            if not is_latest:
                if obf in self.struct_pairing:
                    return f"{prefix}{self.struct_pairing[obf]}"
                return f"{prefix}_OBF"
            else:
                if paired_latest is not None and obf in paired_latest:
                    return f"{prefix}{obf}"
                if obf in self.struct_pairing.values():
                    return f"{prefix}{obf}"
                return f"{prefix}_OBF"

        return re.sub(r"([EFUA]?)(_[0-9a-fA-F]{10})", repl, t)

    def _is_type_compatible(
        self,
        p_type: str,
        l_type: str,
        p_bitfield: bool,
        l_bitfield: bool,
        p_size: int,
        l_size: int,
    ) -> bool:
        if p_bitfield or l_bitfield:
            return p_bitfield and l_bitfield
        if p_type == l_type:
            return True
        if self._normalize_type(p_type, is_latest=False) == self._normalize_type(l_type, is_latest=True):
            return True
        if p_type in ("float", "double") and l_type in ("float", "double"):
            return True
        if "int" in p_type and "int" in l_type and p_size == l_size:
            return True

        p_has_obf = bool(re.search(r"_[0-9a-fA-F]{10}", p_type))
        l_has_obf = bool(re.search(r"_[0-9a-fA-F]{10}", l_type))
        if not (p_has_obf or l_has_obf):
            return False

        if p_type.endswith("*") and l_type.endswith("*"):
            return True
        for tpl in ("TSubclassOf<", "TSoftObjectPtr<", "TSoftClassPtr<", "TWeakObjectPtr<", "TArray<", "TMap<", "TSet<"):
            if p_type.startswith(tpl) and l_type.startswith(tpl):
                return True

        p_obf = re.search(r"^([EFUA]?)_[0-9a-fA-F]{10}$", p_type)
        l_obf = re.search(r"^([EFUA]?)_[0-9a-fA-F]{10}$", l_type)
        if p_obf and l_obf and p_obf.group(1) == l_obf.group(1):
            return True
        return False

    def _diff_struct_pair(self, prev_s: StructInfo, latest_s: StructInfo):
        if prev_s.is_obfuscated and latest_s.is_obfuscated and prev_s.hash_key and latest_s.hash_key:
            self.hash_to_hash[prev_s.hash_key] = latest_s.hash_key
            known_s_name = self.known_mappings.get(prev_s.hash_key)
            if (
                known_s_name
                and known_s_name.strip()
                and not known_s_name.startswith("*")
                and not known_s_name.startswith("_")
            ):
                valid_s_name = known_s_name.strip()
                if latest_s.hash_key not in self.resolved_mappings:
                    self.resolved_mappings[latest_s.hash_key] = valid_s_name
                    self.transitions.append(
                        VersionTransition(
                            prev_hash=prev_s.hash_key,
                            latest_hash=latest_s.hash_key,
                            struct_name=latest_s.clean_name,
                            prop_offset=0,
                            prop_type=latest_s.raw_name,
                            name=valid_s_name,
                        )
                    )

        p_props = prev_s.props
        l_props = latest_s.props

        p_names = [
            self.known_mappings.get(p.hash_key, p.name) if p.is_obfuscated else p.name
            for p in p_props
        ]
        l_names = [
            self.known_mappings.get(p.hash_key, p.name) if p.is_obfuscated else p.name
            for p in l_props
        ]

        p_map = {(p.offset, p.mask): (pi, p, name) for pi, (p, name) in enumerate(zip(p_props, p_names))}

        existing_clean = {p.name for p in l_props if not p.is_obfuscated}
        used_p: Set[int] = set()
        used_l: Set[int] = set()

        # Pass 0: Exact property raw-name / hash identity alignment (handles minor patches and shifted clean props)
        p_raw_by_name: Dict[str, int] = {}
        for pi, p in enumerate(p_props):
            if p.name not in p_raw_by_name:
                p_raw_by_name[p.name] = pi

        for li, l_prop in enumerate(l_props):
            if l_prop.name in p_raw_by_name:
                pi = p_raw_by_name[l_prop.name]
                if pi in used_p:
                    continue
                p_prop = p_props[pi]
                if self._is_type_compatible(
                    p_prop.type,
                    l_prop.type,
                    p_prop.is_bitfield,
                    l_prop.is_bitfield,
                    p_prop.size,
                    l_prop.size,
                ):
                    used_p.add(pi)
                    used_l.add(li)
                    if l_prop.is_obfuscated:
                        prev_hash = p_prop.hash_key
                        latest_hash = l_prop.hash_key
                        if prev_hash:
                            self.hash_to_hash[prev_hash] = latest_hash
                        clean_name = p_names[pi]
                        if (
                            clean_name
                            and clean_name.strip()
                            and not clean_name.startswith("*")
                            and not clean_name.startswith("_")
                        ):
                            valid_name = clean_name.strip()
                            if valid_name not in existing_clean:
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

        # Pass 1: Direct (offset, mask) alignment
        for li, l_prop in enumerate(l_props):
            if li in used_l:
                continue
            key = (l_prop.offset, l_prop.mask)
            if key in p_map:
                pi, p_prop, clean_name = p_map[key]
                if pi in used_p:
                    continue

                type_match = self._is_type_compatible(
                    p_prop.type,
                    l_prop.type,
                    p_prop.is_bitfield,
                    l_prop.is_bitfield,
                    p_prop.size,
                    l_prop.size,
                )

                if type_match:
                    used_p.add(pi)
                    used_l.add(li)
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
                            if valid_name in existing_clean:
                                continue
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

        # Pass 2: Name sequence alignment between anchor properties
        matcher = difflib.SequenceMatcher(None, p_names, l_names)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "replace" and (i2 - i1) == (j2 - j1):
                for pi, li in zip(range(i1, i2), range(j1, j2)):
                    if pi in used_p or li in used_l:
                        continue
                    p_p = p_props[pi]
                    l_p = l_props[li]
                    clean_name = p_names[pi]
                    if not self._is_type_compatible(
                        p_p.type,
                        l_p.type,
                        p_p.is_bitfield,
                        l_p.is_bitfield,
                        p_p.size,
                        l_p.size,
                    ):
                        continue
                    used_p.add(pi)
                    used_l.add(li)
                    if l_p.is_obfuscated:
                        if p_p.hash_key:
                            self.hash_to_hash[p_p.hash_key] = l_p.hash_key
                        if (
                            clean_name
                            and clean_name.strip()
                            and not clean_name.startswith("*")
                            and not clean_name.startswith("_")
                        ):
                            valid_name = clean_name.strip()
                            if valid_name in existing_clean or l_p.hash_key in self.resolved_mappings:
                                continue
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

        # Pass 3: Normalized type-sequence alignment across shifted/added/removed property blocks
        p_types = [self._normalize_type(p.type, is_latest=False) for p in p_props]
        l_types = [self._normalize_type(p.type, is_latest=True) for p in l_props]
        type_matcher = difflib.SequenceMatcher(None, p_types, l_types)
        for tag, i1, i2, j1, j2 in type_matcher.get_opcodes():
            if tag == "equal":
                for pi, li in zip(range(i1, i2), range(j1, j2)):
                    if pi in used_p or li in used_l:
                        continue
                    p_p = p_props[pi]
                    l_p = l_props[li]
                    if abs(p_p.offset - l_p.offset) > 256:
                        continue
                    if not self._is_type_compatible(
                        p_p.type,
                        l_p.type,
                        p_p.is_bitfield,
                        l_p.is_bitfield,
                        p_p.size,
                        l_p.size,
                    ):
                        continue
                    used_p.add(pi)
                    used_l.add(li)
                    if l_p.is_obfuscated:
                        if p_p.hash_key:
                            self.hash_to_hash[p_p.hash_key] = l_p.hash_key
                        if l_p.hash_key in self.resolved_mappings:
                            continue
                        clean_name = p_names[pi]
                        if (
                            clean_name
                            and clean_name.strip()
                            and not clean_name.startswith("*")
                            and not clean_name.startswith("_")
                        ):
                            valid_name = clean_name.strip()
                            if valid_name in existing_clean:
                                continue
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

        existing_names = {p.name for p in h_props if not p.is_obfuscated}
        used_names = set(existing_names)

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
                        name = clean_name.strip()
                        if name not in used_names:
                            used_names.add(name)
                            resolved[h_p.hash_key] = name

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
                            name = clean_name.strip()
                            if name not in used_names:
                                used_names.add(name)
                                resolved[h_p.hash_key] = name

    return resolved
