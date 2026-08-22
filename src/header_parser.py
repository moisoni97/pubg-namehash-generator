import os
import re
from typing import Dict, List, Optional, Any


class PropertyInfo:
    def __init__(
        self,
        name: str,
        prop_type: str,
        offset: int,
        size: int = 0,
        mask: int = 0,
        dim: int = 1,
        is_bitfield: bool = False,
    ):
        self.name = name
        self.type = prop_type
        self.offset = offset
        self.size = size
        self.mask = mask
        self.dim = dim
        self.is_bitfield = is_bitfield
        self.is_obfuscated = self._check_obfuscated(name)
        self.hash_key = f"*{name[1:]}" if self.is_obfuscated else None

    @staticmethod
    def _check_obfuscated(name: str) -> bool:
        if (name.startswith("_") or name.startswith("*")) and len(name) == 11:
            return all(c in "0123456789abcdefABCDEF" for c in name[1:])
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "offset": self.offset,
            "size": self.size,
            "mask": self.mask,
            "dim": self.dim,
            "is_bitfield": self.is_bitfield,
            "is_obfuscated": self.is_obfuscated,
            "hash_key": self.hash_key,
        }


class StructInfo:
    def __init__(
        self,
        raw_name: str,
        raw_super: Optional[str] = None,
        module: str = "",
        props: Optional[List[PropertyInfo]] = None,
    ):
        self.raw_name = raw_name
        self.raw_super = raw_super
        self.clean_name = self._clean_prefix(raw_name)
        self.clean_super = self._clean_prefix(raw_super) if raw_super else None
        self.module = module
        self.props = props or []
        self.is_obfuscated = PropertyInfo._check_obfuscated(raw_name)
        self.hash_key = f"*{raw_name[1:]}" if self.is_obfuscated else None

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
            "module": self.module,
            "is_obfuscated": self.is_obfuscated,
            "hash_key": self.hash_key,
            "props": [p.to_dict() for p in self.props],
        }


class HeaderParser:
    PROP_PATTERN = re.compile(
        r"^([A-Za-z0-9_<>:,\s\*]+)\s+(\w+)(?:\[(\d+)\])?;\s*//\s*(0x[0-9A-Fa-f]+)(?:\((0x[0-9A-Fa-f]+)\))?(?:\s*mask\s*(0x[0-9A-Fa-f]+))?"
    )
    STRUCT_PATTERN = re.compile(
        r"(?:struct|class) (?:alignas\(\d+\) )?(\w+)(?: : public (\w+))?[^{]*\{([^}]+)\}"
    )

    def __init__(self, sdk_dump_path: str):
        self.sdk_dump_path = sdk_dump_path
        self.structs: Dict[str, StructInfo] = {}
        self.raw_structs: Dict[str, StructInfo] = {}

    def parse(self) -> Dict[str, StructInfo]:
        if not os.path.exists(self.sdk_dump_path):
            raise FileNotFoundError(f"SDK dump directory not found: {self.sdk_dump_path}")

        target_dir = self.sdk_dump_path
        dump_subdir = os.path.join(self.sdk_dump_path, "DUMP")
        if os.path.exists(dump_subdir):
            target_dir = dump_subdir

        for root, _, files in os.walk(target_dir):
            for file in files:
                if file.endswith(".h"):
                    file_path = os.path.join(root, file)
                    module_name = file.split("_")[0] if "_" in file else file.replace(".h", "")
                    self._parse_header_file(file_path, module_name)

        return self.structs

    def _parse_header_file(self, file_path: str, module: str):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        for match in self.STRUCT_PATTERN.finditer(content):
            raw_name = match.group(1)
            raw_super = match.group(2)
            body = match.group(3)

            props: List[PropertyInfo] = []
            for line in body.split("\n"):
                line = line.strip()
                if not line or line.startswith("struct ") or line.startswith("class ") or line.startswith("union "):
                    continue
                if "UnknownData" in line or "BITFIELD" in line:
                    continue

                prop_match = self.PROP_PATTERN.match(line)
                if prop_match:
                    prop_type = prop_match.group(1).strip()
                    prop_name = prop_match.group(2).strip()
                    arr_dim = int(prop_match.group(3)) if prop_match.group(3) else 1
                    offset = int(prop_match.group(4), 16)
                    size = int(prop_match.group(5), 16) if prop_match.group(5) else 0
                    mask = int(prop_match.group(6), 16) if prop_match.group(6) else 0
                    is_bitfield = mask > 0 or (" : " in line and "mask" in line)

                    props.append(
                        PropertyInfo(
                            name=prop_name,
                            prop_type=prop_type,
                            offset=offset,
                            size=size,
                            mask=mask,
                            dim=arr_dim,
                            is_bitfield=is_bitfield,
                        )
                    )

            struct_obj = StructInfo(
                raw_name=raw_name,
                raw_super=raw_super,
                module=module,
                props=props,
            )

            self.raw_structs[raw_name] = struct_obj
            self.structs[struct_obj.clean_name] = struct_obj


def parse_sdk(sdk_path: str) -> Dict[str, StructInfo]:
    parser = HeaderParser(sdk_path)
    return parser.parse()
