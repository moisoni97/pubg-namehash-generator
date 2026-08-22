import struct
from typing import Dict, List, Optional, Any, Tuple


class UsmapProperty:
    def __init__(
        self,
        name: str,
        schema_idx: int,
        arr_dim: int,
        type_info: Dict[str, Any],
    ):
        self.name = name
        self.schema_idx = schema_idx
        self.arr_dim = arr_dim
        self.type_info = type_info
        self.is_obfuscated = self._check_obfuscated(name)
        self.hash_key = name if self.is_obfuscated and name.startswith("*") else (f"*{name[1:]}" if self.is_obfuscated else None)

    @staticmethod
    def _check_obfuscated(name: str) -> bool:
        if (name.startswith("*") or name.startswith("_")) and len(name) == 11:
            return all(c in "0123456789abcdefABCDEF" for c in name[1:])
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "schema_idx": self.schema_idx,
            "arr_dim": self.arr_dim,
            "type_info": self.type_info,
            "is_obfuscated": self.is_obfuscated,
            "hash_key": self.hash_key,
        }


class UsmapStruct:
    def __init__(
        self,
        raw_name: str,
        raw_super: Optional[str] = None,
        prop_count: int = 0,
        props: Optional[List[UsmapProperty]] = None,
    ):
        self.raw_name = raw_name
        self.raw_super = raw_super
        self.clean_name = self._clean_prefix(raw_name)
        self.clean_super = self._clean_prefix(raw_super) if raw_super else None
        self.prop_count = prop_count
        self.props = props or []
        self.is_obfuscated = UsmapProperty._check_obfuscated(raw_name)
        self.hash_key = raw_name if self.is_obfuscated and raw_name.startswith("*") else (f"*{raw_name[1:]}" if self.is_obfuscated else None)

    @staticmethod
    def _clean_prefix(name: str) -> str:
        if not name:
            return name
        if len(name) > 1 and name[0] in ("F", "U", "A") and (name[1].isupper() or name[1] == "_"):
            return name[1:]
        return name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_name": self.raw_name,
            "clean_name": self.clean_name,
            "raw_super": self.raw_super,
            "clean_super": self.clean_super,
            "prop_count": self.prop_count,
            "is_obfuscated": self.is_obfuscated,
            "hash_key": self.hash_key,
            "props": [p.to_dict() for p in self.props],
        }


class UsmapParser:
    USMAP_MAGIC = 0x30C4

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.names: List[str] = []
        self.enums: Dict[str, List[Tuple[str, int]]] = {}
        self.structs: Dict[str, UsmapStruct] = {}

    def parse(self) -> Dict[str, UsmapStruct]:
        with open(self.file_path, "rb") as f:
            data = f.read()

        if len(data) < 16:
            raise ValueError(f"Invalid USMAP file: {self.file_path}")

        magic, version, has_ver, comp_method, comp_len, decomp_len = struct.unpack(
            "<HBIBII", data[:16]
        )
        if magic != self.USMAP_MAGIC:
            raise ValueError(f"Invalid USMAP magic: {hex(magic)}")

        offset = 16
        if has_ver != 0:
            offset += 16 + 4 + 4

        payload = data[offset : offset + comp_len]
        if comp_method == 0:
            raw = payload
        elif comp_method == 1:
            import oodle
            raw = oodle.decompress(payload, decomp_len)
        elif comp_method == 2:
            import zstandard
            raw = zstandard.ZstdDecompressor().decompress(payload, max_output_size=decomp_len)
        else:
            raise ValueError(f"Unsupported compression method: {comp_method}")

        p_off = 0
        name_count = struct.unpack_from("<I", raw, p_off)[0]
        p_off += 4
        self.names = []
        for _ in range(name_count):
            nl = struct.unpack_from("<H", raw, p_off)[0]
            p_off += 2
            self.names.append(raw[p_off : p_off + nl].decode("latin-1", errors="replace"))
            p_off += nl

        enum_count = struct.unpack_from("<I", raw, p_off)[0]
        p_off += 4
        for _ in range(enum_count):
            e_idx = struct.unpack_from("<I", raw, p_off)[0]
            p_off += 4
            e_name = self.names[e_idx] if e_idx < len(self.names) else f"Enum_{e_idx}"
            val_count = struct.unpack_from("<H" if version >= 4 else "<B", raw, p_off)[0]
            p_off += 2 if version >= 4 else 1
            vals = []
            for i in range(val_count):
                val_num = struct.unpack_from("<Q", raw, p_off)[0] if version >= 4 else i
                if version >= 4:
                    p_off += 8
                v_idx = struct.unpack_from("<I", raw, p_off)[0]
                p_off += 4
                v_name = self.names[v_idx] if v_idx < len(self.names) else f"Val_{v_idx}"
                vals.append((v_name, val_num))
            self.enums[e_name] = vals

        struct_count = struct.unpack_from("<I", raw, p_off)[0]
        p_off += 4
        for _ in range(struct_count):
            s_idx = struct.unpack_from("<I", raw, p_off)[0]
            p_off += 4
            s_name = self.names[s_idx] if s_idx < len(self.names) else f"Struct_{s_idx}"
            super_idx = struct.unpack_from("<I", raw, p_off)[0]
            p_off += 4
            super_name = (
                self.names[super_idx]
                if super_idx != 0xFFFFFFFF and super_idx < len(self.names)
                else None
            )

            prop_count = struct.unpack_from("<H", raw, p_off)[0]
            p_off += 2
            serializable_count = struct.unpack_from("<H", raw, p_off)[0]
            p_off += 2

            props: List[UsmapProperty] = []
            for _ in range(serializable_count):
                schema_idx = struct.unpack_from("<H", raw, p_off)[0]
                p_off += 2
                arr_dim = raw[p_off]
                p_off += 1
                p_name_idx = struct.unpack_from("<I", raw, p_off)[0]
                p_off += 4
                p_name = self.names[p_name_idx] if p_name_idx < len(self.names) else f"Prop_{p_name_idx}"
                p_type, p_off = self._parse_prop_type(raw, p_off)
                props.append(UsmapProperty(p_name, schema_idx, arr_dim, p_type))

            struct_obj = UsmapStruct(
                raw_name=s_name,
                raw_super=super_name,
                prop_count=prop_count,
                props=props,
            )
            self.structs[struct_obj.clean_name] = struct_obj

        return self.structs

    def _parse_prop_type(self, raw: bytes, p_off: int) -> Tuple[Dict[str, Any], int]:
        prop_type_id = raw[p_off]
        p_off += 1
        type_info: Dict[str, Any] = {"type_id": prop_type_id}

        if prop_type_id in (8, 25):
            inner_type, p_off = self._parse_prop_type(raw, p_off)
            type_info["inner"] = inner_type
        elif prop_type_id == 9:
            s_idx = struct.unpack_from("<I", raw, p_off)[0]
            p_off += 4
            type_info["struct_name"] = self.names[s_idx] if s_idx < len(self.names) else f"Unknown_{s_idx}"
        elif prop_type_id == 24:
            k_type, p_off = self._parse_prop_type(raw, p_off)
            v_type, p_off = self._parse_prop_type(raw, p_off)
            type_info["key_type"] = k_type
            type_info["value_type"] = v_type
        elif prop_type_id == 26:
            inner_type, p_off = self._parse_prop_type(raw, p_off)
            e_idx = struct.unpack_from("<I", raw, p_off)[0]
            p_off += 4
            type_info["inner"] = inner_type
            type_info["enum_name"] = self.names[e_idx] if e_idx < len(self.names) else f"Unknown_{e_idx}"

        return type_info, p_off


def parse_usmap(usmap_path: str) -> Dict[str, UsmapStruct]:
    parser = UsmapParser(usmap_path)
    return parser.parse()
