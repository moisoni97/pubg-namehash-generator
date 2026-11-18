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
    GENERIC_FALLBACK_NAMES = {
        "Ratio",
        "Duration",
        "floats",
        "LocOscillation",
        "AnimMontage",
        "Default",
        "Time",
    }

    COMMON_PRIMITIVE_TYPES = {
        "float",
        "double",
        "bool",
        "int32_t",
        "uint32_t",
        "int64_t",
        "uint64_t",
        "int16_t",
        "uint16_t",
        "int8_t",
        "uint8_t",
        "FName",
        "FString",
        "FText",
        "FVector",
        "FVector2D",
        "FRotator",
        "FTransform",
        "FLinearColor",
        "FColor",
    }

    def __init__(
        self,
        prev_structs: Dict[str, StructInfo],
        latest_structs: Dict[str, StructInfo],
        known_mappings: Optional[Dict[str, str]] = None,
    ):
        self.prev_structs = prev_structs
        self.latest_structs = latest_structs
        self.known_mappings = dict(known_mappings) if known_mappings else {}
        self.transitions: List[VersionTransition] = []
        self.resolved_mappings: Dict[str, str] = {}
        self.prev_resolved_mappings: Dict[str, str] = {}
        self.hash_to_hash: Dict[str, str] = {}
        self.struct_pairing: Dict[str, str] = {}
        self.common_names: Set[str] = set(self.prev_structs.keys()) & set(self.latest_structs.keys())

    @classmethod
    def _is_fallback_name(cls, name: str) -> bool:
        if name in cls.GENERIC_FALLBACK_NAMES:
            return True
        if re.search(r"_(?:[4-9]|\d{2,})$", name) and name.startswith("TargetingFOV_"):
            return True
        return False

    def run_diff(self) -> Dict[str, str]:
        self._pair_obfuscated_structs()
        all_struct_pairs = self._get_all_struct_pairs()

        # Pre-pass: discover properties that were obfuscated in prev_structs and de-obfuscated in latest_structs
        for prev_name, latest_name in all_struct_pairs:
            prev_s = self.prev_structs.get(prev_name)
            latest_s = self.latest_structs.get(latest_name)
            if not prev_s or not latest_s:
                continue
            self._discover_deobfuscated_props(prev_s, latest_s)

        for prev_name, latest_name in all_struct_pairs:
            prev_s = self.prev_structs.get(prev_name)
            latest_s = self.latest_structs.get(latest_name)
            if not prev_s or not latest_s:
                continue

            self._diff_struct_pair(prev_s, latest_s)

        return self.resolved_mappings

    def _get_all_struct_pairs(self) -> List[Tuple[str, str]]:
        pairs = []
        for name in sorted(self.common_names):
            pairs.append((name, name))

        for prev_name, latest_name in self.struct_pairing.items():
            if prev_name != latest_name and (prev_name, latest_name) not in pairs:
                pairs.append((prev_name, latest_name))

        return pairs

    def _pair_obfuscated_structs(self):
        common_names = self.common_names
        paired_latest: Set[str] = set()

        def add_pair(p_s: str, l_s: str) -> bool:
            if not (p_s and l_s and p_s != l_s and (p_s.startswith("_") or l_s.startswith("_"))):
                return False
            if p_s in common_names or l_s in common_names:
                return False
            if p_s.endswith("_C") or l_s.endswith("_C"):
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
            if not (p_clean & l_clean):
                max_len = max(len(p_info.props), len(l_info.props))
                if max_len >= 4 and abs(len(p_info.props) - len(l_info.props)) > max(3, int(0.35 * max_len)):
                    return False
            if p_s not in self.struct_pairing and l_s not in paired_latest:
                self.struct_pairing[p_s] = l_s
                paired_latest.add(l_s)
                return True
            return False

        def norm_obf_generic(t: str) -> str:
            return re.sub(r"_[0-9a-fA-F]{10}", "_OBF", t)

        # Stage 1: Pair structs that share a unique known resolved name (or where one side is de-obfuscated to that name)
        prev_by_known: Dict[str, List[str]] = {}
        for name, s in self.prev_structs.items():
            if name not in common_names and not name.endswith("_C"):
                resolved_n = self.known_mappings.get(s.hash_key) if (s.is_obfuscated and s.hash_key) else (name if not s.is_obfuscated else None)
                if resolved_n:
                    k_name = f"{s.raw_name[0]}:{resolved_n}"
                    prev_by_known.setdefault(k_name, []).append(name)

        latest_by_known: Dict[str, List[str]] = {}
        for name, s in self.latest_structs.items():
            if name not in common_names and not name.endswith("_C"):
                resolved_n = self.known_mappings.get(s.hash_key) if (s.is_obfuscated and s.hash_key) else (name if not s.is_obfuscated else None)
                if resolved_n:
                    k_name = f"{s.raw_name[0]}:{resolved_n}"
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
            if name not in common_names and not name.endswith("_C"):
                sig = clean_sig(s)
                if sig:
                    prev_by_sig.setdefault(sig, []).append(name)

        latest_by_sig: Dict[Any, List[str]] = {}
        for name, s in self.latest_structs.items():
            if name not in common_names and not name.endswith("_C"):
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
            if n not in common_names and not n.endswith("_C") and n not in self.struct_pairing
        ]
        unpaired_latest = [
            n for n in self.latest_structs
            if n not in common_names and not n.endswith("_C") and n not in paired_latest
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

        # Stage 4: Iterative transitive propagation via superclasses, property names, relative offsets, and sequences
        def names_conflict(n1: str, n2: str) -> bool:
            c1 = bool(n1 and not n1.startswith("_") and not n1.startswith("*"))
            c2 = bool(n2 and not n2.startswith("_") and not n2.startswith("*"))
            return c1 and c2 and n1 != n2

        def run_transitive_propagation():
            changed = True
            while changed:
                changed = False
                active_pairs = [(n, n) for n in sorted(common_names)] + list(self.struct_pairing.items())
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
                    if not p_props or not l_props:
                        continue

                    p_resolved_names = [
                        self.known_mappings.get(p.hash_key, p.name) if p.is_obfuscated else p.name
                        for p in p_props
                    ]
                    l_resolved_names = [
                        self.known_mappings.get(p.hash_key, p.name) if p.is_obfuscated else p.name
                        for p in l_props
                    ]

                    # 4b. Clean or known property name match (unique within struct) - strongest signal, runs first!
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
                            p_sub = self._extract_struct_token(p_list[0].type, is_latest=False)
                            l_sub = self._extract_struct_token(l_list[0].type, is_latest=True)
                            if p_sub and l_sub and add_pair(p_sub, l_sub):
                                changed = True

                    # Check if struct property order was shuffled
                    p_clean_ord = [p.name for p in p_props if not p.is_obfuscated]
                    l_clean_ord = [p.name for p in l_props if not p.is_obfuscated]
                    shared_clean = set(p_clean_ord) & set(l_clean_ord)
                    if len(shared_clean) >= 2:
                        if [n for n in p_clean_ord if n in shared_clean] != [n for n in l_clean_ord if n in shared_clean]:
                            continue

                    # 4c. Relative (offset - base_off, mask) match when property index also aligns
                    p_base = p_props[0].offset
                    l_base = l_props[0].offset
                    for idx in range(min(len(p_props), len(l_props))):
                        p_p = p_props[idx]
                        l_p = l_props[idx]
                        if (p_p.offset - p_base) == (l_p.offset - l_base) and p_p.mask == l_p.mask:
                            if not names_conflict(p_resolved_names[idx], l_resolved_names[idx]):
                                p_sub = self._extract_struct_token(p_p.type, is_latest=False)
                                l_sub = self._extract_struct_token(l_p.type, is_latest=True)
                                if p_sub and l_sub and add_pair(p_sub, l_sub):
                                    changed = True

                    # 4d. SequenceMatcher replace blocks on property names
                    matcher = difflib.SequenceMatcher(None, p_resolved_names, l_resolved_names)
                    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                        if tag == "replace" and (i2 - i1) == (j2 - j1):
                            for pi, li in zip(range(i1, i2), range(j1, j2)):
                                if not names_conflict(p_resolved_names[pi], l_resolved_names[li]):
                                    p_sub = self._extract_struct_token(p_props[pi].type, is_latest=False)
                                    l_sub = self._extract_struct_token(l_props[li].type, is_latest=True)
                                    if p_sub and l_sub and add_pair(p_sub, l_sub):
                                        changed = True

                    # 4e. SequenceMatcher equal blocks on normalized types
                    p_norm_t = [norm_obf_generic(p.type) for p in p_props]
                    l_norm_t = [norm_obf_generic(p.type) for p in l_props]
                    t_matcher = difflib.SequenceMatcher(None, p_norm_t, l_norm_t)
                    for tag, i1, i2, j1, j2 in t_matcher.get_opcodes():
                        if tag == "equal":
                            for pi, li in zip(range(i1, i2), range(j1, j2)):
                                if not names_conflict(p_resolved_names[pi], l_resolved_names[li]):
                                    if abs((p_props[pi].offset - p_base) - (l_props[li].offset - l_base)) <= 256:
                                        p_sub = self._extract_struct_token(p_props[pi].type, is_latest=False)
                                        l_sub = self._extract_struct_token(l_props[li].type, is_latest=True)
                                        if p_sub and l_sub and add_pair(p_sub, l_sub):
                                            changed = True

        run_transitive_propagation()

        # Stage 5: Unique layout/type-sequence signature for remaining structs with >= 2 properties
        def layout_sig(s: StructInfo, is_latest: bool):
            if len(s.props) < 2:
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

        while True:
            p_layout: Dict[Any, List[str]] = {}
            for name, s in self.prev_structs.items():
                if name not in common_names and not name.endswith("_C") and name not in self.struct_pairing:
                    sig = layout_sig(s, is_latest=False)
                    if sig:
                        p_layout.setdefault(sig, []).append(name)

            l_layout: Dict[Any, List[str]] = {}
            for name, s in self.latest_structs.items():
                if name not in common_names and not name.endswith("_C") and name not in paired_latest:
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
            else:
                break

        # Transfer known or newly de-obfuscated struct/class/enum hash names across paired structs
        for p_s_name, l_s_name in self.struct_pairing.items():
            p_s = self.prev_structs.get(p_s_name)
            l_s = self.latest_structs.get(l_s_name)
            if not p_s or not l_s:
                continue

            if p_s.is_obfuscated and not l_s.is_obfuscated and p_s.hash_key:
                # Struct was obfuscated in prev SDK and de-obfuscated in latest SDK!
                clean_s_name = l_s.clean_name
                if clean_s_name and not clean_s_name.startswith("_") and not clean_s_name.endswith("_C"):
                    self.prev_resolved_mappings[p_s.hash_key] = clean_s_name
                    self.known_mappings[p_s.hash_key] = clean_s_name
            elif not p_s.is_obfuscated and l_s.is_obfuscated and l_s.hash_key:
                # Struct was clean in prev SDK and obfuscated in latest SDK!
                clean_s_name = p_s.clean_name
                if clean_s_name and not clean_s_name.startswith("_") and not clean_s_name.endswith("_C"):
                    self.resolved_mappings[l_s.hash_key] = clean_s_name
                    self.transitions.append(
                        VersionTransition(
                            prev_hash=None,
                            latest_hash=l_s.hash_key,
                            struct_name=l_s.clean_name,
                            prop_offset=0,
                            prop_type=l_s.raw_name,
                            name=clean_s_name,
                        )
                    )
            elif p_s.is_obfuscated and l_s.is_obfuscated and p_s.hash_key and l_s.hash_key:
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
        m = re.search(r"[EFUA]?(_[0-9a-fA-F]{10})", t)
        if m:
            return m.group(1)
        return None

    def _extract_struct_token(self, t: str, is_latest: bool) -> Optional[str]:
        m = re.search(r"[EFUA]?(_[0-9a-fA-F]{10})", t)
        if m:
            return m.group(1)
        target_map = self.latest_structs if is_latest else self.prev_structs
        for m_c in re.finditer(r"\b([FUA])([A-Z]\w+)", t):
            cand = m_c.group(2)
            if cand in target_map and cand not in self.common_names and not cand.endswith("_C"):
                return cand
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

        res = re.sub(r"([EFUA]?)(_[0-9a-fA-F]{10})", repl, t)
        if not is_latest:
            for m_c in re.finditer(r"\b([FUA])([A-Z]\w+)", res):
                cand = m_c.group(2)
                if cand in self.struct_pairing:
                    res = res.replace(f"{m_c.group(1)}{cand}", f"{m_c.group(1)}{self.struct_pairing[cand]}")
        return res

    def _generic_normalize_type(self, t: str, is_latest: bool) -> str:
        res = re.sub(r"([EFUA]?)_[0-9a-fA-F]{10}", r"\1_OBF", t)
        target_map = self.latest_structs if is_latest else self.prev_structs
        for m_c in re.finditer(r"\b([FUA])([A-Z]\w+)", res):
            cand = m_c.group(2)
            if cand in target_map and cand not in self.common_names:
                res = re.sub(rf"\b{m_c.group(1)}{cand}\b", f"{m_c.group(1)}_OBF", res)
        if is_latest:
            res = re.sub(r"\bE[A-Z]\w+\b", "E_OBF", res)
        return res

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

        p_gen = re.sub(r"([EFUA]?)_[0-9a-fA-F]{10}", r"\1_OBF", p_type)
        l_gen = re.sub(r"([EFUA]?)_[0-9a-fA-F]{10}", r"\1_OBF", l_type)
        if p_gen == l_gen:
            return True

        if self._generic_normalize_type(p_type, is_latest=False) == self._generic_normalize_type(l_type, is_latest=True):
            return True

        return False

    def _compute_monotonic_anchors(
        self,
        p_props: List[PropertyInfo],
        l_props: List[PropertyInfo],
        matched_pairs: List[Tuple[int, int]],
    ) -> Tuple[List[Tuple[int, int]], bool]:
        if not matched_pairs:
            return [], False

        sorted_pairs = sorted(matched_pairs, key=lambda x: x[0])
        is_shuffled = any(
            sorted_pairs[k][1] > sorted_pairs[k + 1][1]
            for k in range(len(sorted_pairs) - 1)
        )

        if not is_shuffled:
            return sorted_pairs, False

        # Longest increasing subsequence on li weighted by local offset consistency
        n = len(sorted_pairs)
        dp = [1] * n
        parent = [-1] * n
        for i in range(n):
            pi_i, li_i = sorted_pairs[i]
            for j in range(i):
                pi_j, li_j = sorted_pairs[j]
                if li_j < li_i:
                    if dp[j] + 1 > dp[i]:
                        dp[i] = dp[j] + 1
                        parent[i] = j

        best_end = max(range(n), key=lambda i: dp[i])
        chain: List[Tuple[int, int]] = []
        curr = best_end
        while curr != -1:
            chain.append(sorted_pairs[curr])
            curr = parent[curr]
        chain.reverse()
        return chain, True

    def _discover_deobfuscated_props(self, prev_s: StructInfo, latest_s: StructInfo):
        p_props = prev_s.props
        l_props = latest_s.props
        if not p_props or not l_props or latest_s.clean_name.endswith("_C"):
            return

        p_clean_ord = [p.name for p in p_props if not p.is_obfuscated]
        l_clean_ord = [p.name for p in l_props if not p.is_obfuscated]
        shared = set(p_clean_ord) & set(l_clean_ord)
        if len(shared) >= 2 and [n for n in p_clean_ord if n in shared] != [n for n in l_clean_ord if n in shared]:
            return

        if len(p_props) == len(l_props):
            p_base = p_props[0].offset
            l_base = l_props[0].offset
            for pp, lp in zip(p_props, l_props):
                if pp.is_obfuscated and not lp.is_obfuscated and pp.hash_key:
                    if (pp.offset - p_base) == (lp.offset - l_base) and pp.mask == lp.mask:
                        if self._is_type_compatible(pp.type, lp.type, pp.is_bitfield, lp.is_bitfield, pp.size, lp.size):
                            if not re.match(r"^_[0-9a-fA-F]{10}_[0-9A-Fa-f]{32}$", lp.name):
                                self.prev_resolved_mappings[pp.hash_key] = lp.name
                                self.known_mappings[pp.hash_key] = lp.name

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
        if not p_props or not l_props:
            return

        p_names = [
            self.known_mappings.get(p.hash_key, p.name) if p.is_obfuscated else p.name
            for p in p_props
        ]
        l_names = [
            self.known_mappings.get(p.hash_key, p.name) if p.is_obfuscated else p.name
            for p in l_props
        ]

        existing_clean = {p.name for p in l_props if not p.is_obfuscated}
        used_p: Set[int] = set()
        used_l: Set[int] = set()
        matched_pairs: List[Tuple[int, int]] = []

        def record_match(pi: int, li: int):
            used_p.add(pi)
            used_l.add(li)
            matched_pairs.append((pi, li))
            p_prop = p_props[pi]
            l_prop = l_props[li]

            # Transfer known obfuscated type tokens (e.g. obfuscated enums or unindexed structs)
            p_t_tokens = re.findall(r"([EFUA])_([0-9a-fA-F]{10})", p_prop.type)
            l_t_tokens = re.findall(r"([EFUA])_([0-9a-fA-F]{10})", l_prop.type)
            if len(p_t_tokens) == len(l_t_tokens):
                for (p_pfx, p_hex), (l_pfx, l_hex) in zip(p_t_tokens, l_t_tokens):
                    if p_pfx == l_pfx:
                        p_th = f"*{p_hex}"
                        l_th = f"*{l_hex}"
                        self.hash_to_hash[p_th] = l_th
                        t_known = self.known_mappings.get(p_th)
                        if (
                            t_known
                            and t_known.strip()
                            and not t_known.startswith("*")
                            and not t_known.startswith("_")
                            and l_th not in self.resolved_mappings
                        ):
                            self.resolved_mappings[l_th] = t_known.strip()

            if p_prop.is_obfuscated and not l_prop.is_obfuscated and p_prop.hash_key:
                if not re.match(r"^_[0-9a-fA-F]{10}_[0-9A-Fa-f]{32}$", l_prop.name):
                    self.prev_resolved_mappings[p_prop.hash_key] = l_prop.name
                    self.known_mappings[p_prop.hash_key] = l_prop.name
                return

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
                    if valid_name in existing_clean:
                        return
                    if latest_hash in self.resolved_mappings:
                        curr_name = self.resolved_mappings[latest_hash]
                        if curr_name == valid_name:
                            return
                        if not self._is_fallback_name(curr_name) and self._is_fallback_name(valid_name):
                            return
                        if not self._is_fallback_name(curr_name):
                            return
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

        # Pass 0: Exact property raw-name / hash identity alignment
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
                    record_match(pi, li)

        # Compute monotonic anchor chain and whether the struct is layout-shuffled
        anchors, is_shuffled = self._compute_monotonic_anchors(p_props, l_props, matched_pairs)

        # Build monotonic intervals: [ (pi_start, li_start, delta_start, pi_end, li_end, delta_end), ... ]
        def get_monotonic_intervals() -> List[Tuple[int, int, Optional[int], int, int, Optional[int]]]:
            curr_anchors, _ = self._compute_monotonic_anchors(p_props, l_props, matched_pairs)
            if not curr_anchors:
                if is_shuffled:
                    return []
                base_delta = l_props[0].offset - p_props[0].offset
                return [(-1, -1, base_delta, len(p_props), len(l_props), base_delta)]

            intervals = []
            first_pi, first_li = curr_anchors[0]
            first_delta = l_props[first_li].offset - p_props[first_pi].offset
            if first_pi > 0 and first_li > 0 and not is_shuffled:
                base_delta = l_props[0].offset - p_props[0].offset
                intervals.append((-1, -1, base_delta, first_pi, first_li, first_delta))

            for k in range(len(curr_anchors) - 1):
                pa, la = curr_anchors[k]
                pb, lb = curr_anchors[k + 1]
                if pb - pa <= 1 or lb - la <= 1:
                    continue
                da = l_props[la].offset - p_props[pa].offset
                db = l_props[lb].offset - p_props[pb].offset
                if is_shuffled and da != db and (pb - pa) > 10:
                    continue
                intervals.append((pa, la, da, pb, lb, db))

            last_pi, last_li = curr_anchors[-1]
            last_delta = l_props[last_li].offset - p_props[last_pi].offset
            if last_pi < len(p_props) - 1 and last_li < len(l_props) - 1:
                if not is_shuffled or (len(curr_anchors) >= 2 and (l_props[curr_anchors[-2][1]].offset - p_props[curr_anchors[-2][0]].offset) == last_delta):
                    intervals.append((last_pi, last_li, last_delta, len(p_props), len(l_props), last_delta))

            return intervals

        # Pass 1: Anchor-Guided Local-Delta Offset Alignment
        for pa, la, da, pb, lb, db in get_monotonic_intervals():
            if da is not None and da == db:
                # Uniform offset shift across the entire interval
                p_sub_map = {
                    (p_props[pi].offset + da, p_props[pi].mask): pi
                    for pi in range(pa + 1, pb)
                    if pi not in used_p
                }
                for li in range(la + 1, lb):
                    if li in used_l:
                        continue
                    l_prop = l_props[li]
                    key = (l_prop.offset, l_prop.mask)
                    if key in p_sub_map:
                        pi = p_sub_map[key]
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
                            record_match(pi, li)
            else:
                # Propagate forward from start of interval with delta da
                if da is not None:
                    p_sub_a = {
                        (p_props[pi].offset + da, p_props[pi].mask): pi
                        for pi in range(pa + 1, pb)
                        if pi not in used_p
                    }
                    for li in range(la + 1, lb):
                        if li in used_l:
                            continue
                        l_prop = l_props[li]
                        key = (l_prop.offset, l_prop.mask)
                        if key in p_sub_a:
                            pi = p_sub_a[key]
                            if pi not in used_p and self._is_type_compatible(
                                p_props[pi].type,
                                l_prop.type,
                                p_props[pi].is_bitfield,
                                l_prop.is_bitfield,
                                p_props[pi].size,
                                l_prop.size,
                            ):
                                record_match(pi, li)
                        else:
                            break

                # Propagate backward from end of interval with delta db
                if db is not None:
                    p_sub_b = {
                        (p_props[pi].offset + db, p_props[pi].mask): pi
                        for pi in range(pa + 1, pb)
                        if pi not in used_p
                    }
                    for li in range(lb - 1, la, -1):
                        if li in used_l:
                            continue
                        l_prop = l_props[li]
                        key = (l_prop.offset, l_prop.mask)
                        if key in p_sub_b:
                            pi = p_sub_b[key]
                            if pi not in used_p and self._is_type_compatible(
                                p_props[pi].type,
                                l_prop.type,
                                p_props[pi].is_bitfield,
                                l_prop.is_bitfield,
                                p_props[pi].size,
                                l_prop.size,
                            ):
                                record_match(pi, li)
                        else:
                            break

        # Pass 2: Equal-count gap & name sequence alignment within monotonic intervals
        for pa, la, da, pb, lb, db in get_monotonic_intervals():
            if (pb - pa) == (lb - la):
                for pi, li in zip(range(pa + 1, pb), range(la + 1, lb)):
                    if pi in used_p or li in used_l:
                        continue
                    p_p = p_props[pi]
                    l_p = l_props[li]
                    if self._is_type_compatible(
                        p_p.type,
                        l_p.type,
                        p_p.is_bitfield,
                        l_p.is_bitfield,
                        p_p.size,
                        l_p.size,
                    ):
                        record_match(pi, li)
            else:
                sub_p_names = p_names[pa + 1 : pb]
                sub_l_names = l_names[la + 1 : lb]
                matcher = difflib.SequenceMatcher(None, sub_p_names, sub_l_names)
                for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                    if tag == "replace" and (i2 - i1) == (j2 - j1):
                        for pi, li in zip(range(pa + 1 + i1, pa + 1 + i2), range(la + 1 + j1, la + 1 + j2)):
                            if pi in used_p or li in used_l:
                                continue
                            p_p = p_props[pi]
                            l_p = l_props[li]
                            if self._is_type_compatible(
                                p_p.type,
                                l_p.type,
                                p_p.is_bitfield,
                                l_p.is_bitfield,
                                p_p.size,
                                l_p.size,
                            ):
                                record_match(pi, li)

        # Pass 3: Normalized type-sequence alignment within monotonic intervals
        p_types = [self._normalize_type(p.type, is_latest=False) for p in p_props]
        l_types = [self._normalize_type(p.type, is_latest=True) for p in l_props]
        for pa, la, da, pb, lb, db in get_monotonic_intervals():
            sub_p_t = p_types[pa + 1 : pb]
            sub_l_t = l_types[la + 1 : lb]
            type_matcher = difflib.SequenceMatcher(None, sub_p_t, sub_l_t)
            ref_delta = da if da is not None else (db if db is not None else 0)
            for tag, i1, i2, j1, j2 in type_matcher.get_opcodes():
                if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
                    for pi, li in zip(range(pa + 1 + i1, pa + 1 + i2), range(la + 1 + j1, la + 1 + j2)):
                        if pi in used_p or li in used_l:
                            continue
                        p_p = p_props[pi]
                        l_p = l_props[li]
                        ok_dist = abs((l_p.offset - p_p.offset) - ref_delta) <= 256 or (
                            db is not None and abs((l_p.offset - p_p.offset) - db) <= 256
                        )
                        if not ok_dist:
                            continue
                        if self._is_type_compatible(
                            p_p.type,
                            l_p.type,
                            p_p.is_bitfield,
                            l_p.is_bitfield,
                            p_p.size,
                            l_p.size,
                        ):
                            record_match(pi, li)

        # Pass 4: Unique property type within struct (handles layout-shuffled structs like ATslCharacter, FWeaponData, AActor)
        if not latest_s.clean_name.endswith("_C"):
            p_all_type_counts: Dict[Tuple[str, bool, int], int] = {}
            for p, t in zip(p_props, p_types):
                tk = (t, p.is_bitfield, p.mask)
                p_all_type_counts[tk] = p_all_type_counts.get(tk, 0) + 1
            l_all_type_counts: Dict[Tuple[str, bool, int], int] = {}
            for p, t in zip(l_props, l_types):
                tk = (t, p.is_bitfield, p.mask)
                l_all_type_counts[tk] = l_all_type_counts.get(tk, 0) + 1

            unmatched_p_by_t: Dict[Tuple[str, bool, int], List[int]] = {}
            for pi, (p, t) in enumerate(zip(p_props, p_types)):
                if pi not in used_p and "_OBF" not in t:
                    unmatched_p_by_t.setdefault((t, p.is_bitfield, p.mask), []).append(pi)

            unmatched_l_by_t: Dict[Tuple[str, bool, int], List[int]] = {}
            for li, (p, t) in enumerate(zip(l_props, l_types)):
                if li not in used_l and "_OBF" not in t:
                    unmatched_l_by_t.setdefault((t, p.is_bitfield, p.mask), []).append(li)

            for tk, p_idx_list in unmatched_p_by_t.items():
                l_idx_list = unmatched_l_by_t.get(tk, [])
                if len(p_idx_list) == 1 and len(l_idx_list) == 1:
                    t = tk[0]
                    if t in self.COMMON_PRIMITIVE_TYPES:
                        if p_all_type_counts.get(tk, 0) > 1 or l_all_type_counts.get(tk, 0) > 1:
                            continue
                    pi = p_idx_list[0]
                    li = l_idx_list[0]
                    p_p = p_props[pi]
                    l_p = l_props[li]
                    if self._is_type_compatible(
                        p_p.type,
                        l_p.type,
                        p_p.is_bitfield,
                        l_p.is_bitfield,
                        p_p.size,
                        l_p.size,
                    ):
                        record_match(pi, li)

            # Pass 5: Adjacent neighbor relative-offset propagation from matched anchors (handles paired fields in shuffled structs)
            changed_neighbor = True
            while changed_neighbor:
                changed_neighbor = False
                for pi_a, li_a in list(matched_pairs):
                    p_anchor = p_props[pi_a]
                    l_anchor = l_props[li_a]
                    for step in (-1, 1):
                        pi_n = pi_a + step
                        li_n = li_a + step
                        if 0 <= pi_n < len(p_props) and 0 <= li_n < len(l_props):
                            if pi_n not in used_p and li_n not in used_l:
                                p_n = p_props[pi_n]
                                l_n = l_props[li_n]
                                if (p_n.offset - p_anchor.offset) == (l_n.offset - l_anchor.offset) and p_n.mask == l_n.mask:
                                    if self._is_type_compatible(
                                        p_n.type,
                                        l_n.type,
                                        p_n.is_bitfield,
                                        l_n.is_bitfield,
                                        p_n.size,
                                        l_n.size,
                                    ):
                                        record_match(pi_n, li_n)
                                        changed_neighbor = True


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
