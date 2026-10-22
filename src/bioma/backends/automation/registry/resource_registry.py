# src/capabilities/registry/resource_registry.py

"""
Resource Registry - Manages ID mapping for instruments and consumables
Supports:
1. Generate concise list (for LLM)
2. Intelligent keyword-to-ID matching
3. ID validity verification
"""

import csv
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass

from bioma.backends.automation import settings
from bioma.backends.automation.registry.base import BaseRegistry


@dataclass
class Instrument:
    """Instrument data structure"""
    resource_id: int
    name: str
    description: str
    model: str
    keywords: List[str]  


@dataclass
class Consumable:
    """Consumable data structure"""
    resource_id: int
    name: str
    type: str
    keywords: List[str]


class ResourceRegistry(BaseRegistry):
    """
    Resource Registry Singleton
    Responsible for loading, caching, and querying instrument/consumable information
    """

    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self.instruments: Dict[int, Instrument] = {}
        self.consumables: Dict[int, Consumable] = {}
    
        self.instrument_keyword_map: Dict[str, int] = {}
        self.consumable_keyword_map: Dict[str, int] = {}
        self.instrument_primary_alias: Dict[int, str] = {}
        self.consumable_primary_alias: Dict[int, str] = {}
        self.instrument_alias_texts: Dict[int, List[str]] = {}
        self.consumable_alias_texts: Dict[int, List[str]] = {}
        
        self.valid_instrument_ids: Set[int] = set()
        self.valid_consumable_ids: Set[int] = set()
        
        self._load_aliases_from_en_json()
        self._load_instruments()
        self._load_consumables()
        self._build_keyword_maps()
        
        self._initialized = True
        
        self.external_device_categories = self._build_external_categories()
        print(f"[OK] ResourceRegistry initialized: {len(self.instruments)} instruments, {len(self.consumables)} consumables")

    def _normalize_text(self, text: str) -> str:
        if not text:
            return ""
        t = text.lower().strip()
        t = t.replace("μ", "u").replace("µ", "u")
        t = re.sub(r"[_/\\\-]+", " ", t)
        t = re.sub(r"\s+", " ", t)
        return t

    def _safe_load_json_list(self, path: str) -> List[Dict[str, Any]]:
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _collect_alias_values(self, row: Dict[str, Any], fields: List[str]) -> List[str]:
        vals: List[str] = []
        for field in fields:
            raw = row.get(field)
            if raw is None:
                continue
            val = str(raw).strip()
            if val:
                vals.append(val)
        return vals

    def _load_aliases_from_en_json(self):
        """Load optional English alias resources for bilingual matching."""
        instruments_en = self._safe_load_json_list(str(settings.INSTRUMENTS_EN_PATH))
        consumables_en = self._safe_load_json_list(str(settings.CONSUMABLES_EN_PATH))

        for row in instruments_en:
            try:
                rid = int(row.get("resourceId", 0))
            except Exception:
                continue
            aliases = self._collect_alias_values(
                row,
                ["deviceName", "model", "supplier", "description", "specifications"],
            )
            if aliases:
                self.instrument_alias_texts[rid] = aliases
                self.instrument_primary_alias[rid] = aliases[0]

        for row in consumables_en:
            try:
                rid = int(row.get("resourceId", 0))
            except Exception:
                continue
            aliases = self._collect_alias_values(
                row,
                ["name", "materialName", "type", "supplier", "description", "pairedDevices"],
            )
            if aliases:
                self.consumable_alias_texts[rid] = aliases
                self.consumable_primary_alias[rid] = aliases[0]
    
    
    def _build_external_categories(self) -> Dict[str, List[str]]:
        """Build external device categories (for reference only, no restrictions)"""
        return {
            "Molecular Biology": [
                "Gel Electrophoresis System", "Gel Imager", "Electroporator", "UV Crosslinker",
                "Nucleic Acid Extractor", "Gene Gun", "Sequencer", "Southern/Northern Blot System"
            ],
            "Cell Biology": [
                "Flow Cytometer", "Cell Sorter", "Inverted Microscope", "Fluorescence Microscope",
                "Confocal Microscope", "CO2 Incubator", "Clean Bench", "Cell Counter"
            ],
            "Protein-Related": [
                "Western Blot System", "Protein Purification System", "FPLC", "HPLC",
                "Mass Spectrometer", "Ultrasonic Homogenizer", "High-Pressure Homogenizer", "Freeze Dryer"
            ],
            "Microbiology-Related": [
                "Fermenter", "Anaerobic Workstation", "Strain Preservation System", "Colony Counter"
            ],
            "General Equipment": [
                "Balance", "pH Meter", "Spectrophotometer", "Rotary Evaporator",
                "Autoclave", "Oven", "Water Bath", "Magnetic Stirrer"
            ]
        }
        
    def is_known_external_device(self, device_name: str) -> Tuple[bool, str]:
        """
        Check if the device belongs to a known external device category
        Returns: (is_known, category)
        """
        device_lower = device_name.lower()
        
        for category, devices in self.external_device_categories.items():
            for device in devices:
                if device.lower() in device_lower or device_lower in device.lower():
                    return True, category
        
        return False, "Other"
    
    def get_external_device_summary(self) -> str:
        """Generate a summary of external device categories"""
        lines = ["## Common External Device Categories (for reference only; any device can be used) \n"]
        
        for category, devices in self.external_device_categories.items():
            lines.append(f"**{category}**: {', '.join(devices[:5])}{'...' if len(devices) > 5 else ''}")
        
        return '\n'.join(lines)
    
    def validate_node_resource(self, node: Dict) -> Tuple[bool, str]:
        """
        Validate the resource configuration of a node
        Returns: (is_valid, error/warning message)
        """
        resource_id = node.get("resourceId")
        parameters = node.get("parameters", {})
        
        if resource_id and resource_id > 0:
            if resource_id in self.valid_instrument_ids:
                return True, ""
            else:
                return False, f"Invalid resourceId: {resource_id}"
        
        if resource_id == -1 or resource_id is None:
            operation_type = parameters.get("operationType", "manual")
            
            if operation_type == "external":
                device_name = parameters.get("deviceName", "")
                if not device_name:
                    return False, "External device node missing deviceName"
                
                description = parameters.get("description", "")
                if not description:
                    return False, "External device node missing description"
                
                return True, ""
            
            elif operation_type == "manual":
                description = parameters.get("description", parameters.get("notes", ""))
                if not description:
                    return False, "Manual operation node missing description or notes"
                
                return True, ""
            
            else:
                return False, f"Unknown operationType: {operation_type}"
        
        return False, f"Invalid resourceId type: {type(resource_id)}"
    
    
    def _load_instruments(self):
        """Load instrument data"""
        file_path = settings.INSTRUMENTS_PATH
        if not os.path.exists(file_path):
            print(f"[Warning] Instrument CSV does not exist: {file_path}; loading {settings.INSTRUMENTS_EN_PATH}")
            for row in self._safe_load_json_list(str(settings.INSTRUMENTS_EN_PATH)):
                try:
                    if str(row.get("statusCode", "")).casefold() != "available":
                        continue
                    rid = int(row.get('resourceId', 0))
                    name = str(row.get('deviceName', '')).strip()
                    desc = str(row.get('description', '')).strip()
                    model = str(row.get('model', '')).strip()
                    alias_texts = self.instrument_alias_texts.get(rid, [])

                    keywords = self._extract_keywords(name, desc, model, *alias_texts)

                    self.instruments[rid] = Instrument(
                        resource_id=rid,
                        name=name or self.instrument_primary_alias.get(rid, f"Instrument {rid}"),
                        description=desc,
                        model=model,
                        keywords=keywords
                    )
                    self.valid_instrument_ids.add(rid)
                except Exception as e:
                    print(f"[Warning] Failed to parse instrument JSON data: {row}, Error: {e}")
            return
        
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    if str(row.get("statusCode", "")).casefold() != "available":
                        continue
                    rid = int(row.get('resourceId', 0))
                    name = self.instrument_primary_alias.get(rid, row.get('deviceName', '').strip())
                    desc = row.get('description', '').strip()
                    model = row.get('model', '').strip()
                    alias_texts = self.instrument_alias_texts.get(rid, [])
                    
                    keywords = self._extract_keywords(name, desc, model, *alias_texts)
                    
                    self.instruments[rid] = Instrument(
                        resource_id=rid,
                        name=name,
                        description=desc,
                        model=model,
                        keywords=keywords
                    )
                    self.valid_instrument_ids.add(rid)
                except Exception as e:
                    print(f"[Warning] Failed to parse instrument data: {row}, Error: {e}")
    
    def _load_consumables(self):
        """Load consumable data"""
        file_path = settings.CONSUMABLES_PATH
        if not os.path.exists(file_path):
            print(f"[Warning] Consumable CSV does not exist: {file_path}; loading {settings.CONSUMABLES_EN_PATH}")
            for row in self._safe_load_json_list(str(settings.CONSUMABLES_EN_PATH)):
                try:
                    rid = int(row.get('resourceId', 0))
                    name = str(row.get('materialName') or row.get('name') or '').strip()
                    type_ = str(row.get('type', '')).strip()
                    desc = str(row.get('description', '')).strip()
                    alias_texts = self.consumable_alias_texts.get(rid, [])

                    keywords = self._extract_keywords(name, type_, desc, *alias_texts)

                    self.consumables[rid] = Consumable(
                        resource_id=rid,
                        name=name or self.consumable_primary_alias.get(rid, f"Consumable {rid}"),
                        type=type_,
                        keywords=keywords
                    )
                    self.valid_consumable_ids.add(rid)
                except Exception as e:
                    print(f"[Warning] Failed to parse consumable JSON data: {row}, error: {e}")
            return
        
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rid = int(row.get('resourceId', 0))
                    name = self.consumable_primary_alias.get(rid, row.get('materialName', '').strip())
                    type_ = row.get('type', '').strip()
                    desc = row.get('description', '').strip()
                    alias_texts = self.consumable_alias_texts.get(rid, [])
                    
                    keywords = self._extract_keywords(name, type_, desc, *alias_texts)
                    
                    self.consumables[rid] = Consumable(
                        resource_id=rid,
                        name=name,
                        type=type_,
                        keywords=keywords
                    )
                    self.valid_consumable_ids.add(rid)
                except Exception as e:
                    print(f"[Warning] Failed to parse consumable data: {row}, error: {e}")
    
    def _extract_keywords(self, *texts) -> List[str]:
        """Extract keywords from text (filter meaningless phrases)"""
        keywords = []
        
        stop_words = {'a', 'i', 'x', 'v', 'the', 'of', 'and', 'or', 'for', 'to', 'in', 'on', 'at'}
        
        for text in texts:
            if not text:
                continue
            
            text.lower()
            
            cn_keywords = re.findall(r'[\u4e00-\u9fa5]{2,}', text)
            
            en_keywords = re.findall(r'[a-zA-Z]{2,}', text)
            en_keywords = [k.lower() for k in en_keywords if k.lower() not in stop_words]
            
            keywords.extend(cn_keywords)
            keywords.extend(en_keywords)
        
        return list(set(keywords))
    
    def _build_keyword_maps(self):
        """Build keyword-to-ID mappings"""
        # Instrument keyword mapping
        for rid, inst in self.instruments.items():
            for kw in inst.keywords:
                if kw not in self.instrument_keyword_map:
                    self.instrument_keyword_map[kw] = rid
        
        # Consumable keyword mapping
        for rid, cons in self.consumables.items():
            for kw in cons.keywords:
                if kw not in self.consumable_keyword_map:
                    self.consumable_keyword_map[kw] = rid
    
    # ==================== 查询接口 ====================
    
    def is_valid_instrument_id(self, rid: int) -> bool:
        return rid in self.valid_instrument_ids or rid == -1
    
    def is_valid_consumable_id(self, rid: int) -> bool:
        return rid in self.valid_consumable_ids
    
    def match_instrument_by_text(self, text: str) -> Optional[int]:
        """
        Match the most appropriate instrument ID based on text
        Matching Priority:
        1. Exact match of instrument name
        2. Keyword match
        3. Fuzzy match
        """
        text_norm = self._normalize_text(text)
        
        # 1. Exact name match
        for rid, inst in self.instruments.items():
            candidates = [inst.name]
            if rid in self.instrument_primary_alias:
                candidates.append(self.instrument_primary_alias[rid])
            for c in candidates:
                if self._normalize_text(c) == text_norm:
                    return rid
        
        # 2. Name contains match
        for rid, inst in self.instruments.items():
            candidates = [inst.name]
            if rid in self.instrument_primary_alias:
                candidates.append(self.instrument_primary_alias[rid])
            for c in candidates:
                c_norm = self._normalize_text(c)
                if c_norm and (c_norm in text_norm or text_norm in c_norm):
                    return rid
        
        # 3. Keyword match (with weight)
        scores: Dict[int, int] = {}
        text_keywords = self._extract_keywords(text)
        
        for kw in text_keywords:
            if kw in self.instrument_keyword_map:
                rid = self.instrument_keyword_map[kw]
                scores[rid] = scores.get(rid, 0) + 1
        
        if scores:
            return max(scores, key=scores.get)
        
        # 4. Specific rule matching       
        keyword_rules = {
            "pipetting": ["pipetting", "pipet", "liquid handling", "dispensing"],
            "pcr": ["pcr", "thermal cycling", "amplification", "thermocycl"],
            "centrifugation": ["centrifugation", "centrifuge"],
            "sealing": ["sealing", "seal", "sealer"],
            "peeling": ["peeling", "peel", "desealer"],
            "incubation": ["incubation", "incubate", "cultivation", "shaker"],
            "microplate reading": ["microplate reading", "reader", "spectra"],
            "shaking": ["shaking", "vortex", "mixing"],
            "robotic arm": ["robotic arm", "robot", "arm", "scara"],
            "dispensing": ["dispensing", "liquid dispensing"],
            "plate washing": ["plate washing", "wash"],
            "pipetting_zh": ["移液", "自动化移液"],
            "pcr_zh": ["扩增", "pcr仪", "聚合酶链式反应"],
            "centrifuge_zh": ["离心", "离心机"],
            "sealer_zh": ["封膜"],
            "peeler_zh": ["撕膜"],
            "incubation_zh": ["孵育", "培养"],
            "reader_zh": ["酶标", "检测"],
            "washer_zh": ["洗板"],
        }
        
        for rule_key, rule_keywords in keyword_rules.items():
            if any(self._normalize_text(rk) in text_norm or rk in text for rk in rule_keywords):
                for rid, inst in self.instruments.items():
                    alias_blob = " ".join(self.instrument_alias_texts.get(rid, []))
                    inst_text = f"{inst.name} {inst.description} {alias_blob}"
                    inst_norm = self._normalize_text(inst_text)
                    if any(self._normalize_text(rk) in inst_norm or rk in inst_text for rk in rule_keywords):
                        return rid
        
        return None
    
    def match_consumable_by_text(self, text: str) -> Optional[int]:
        """Match consumable ID based on text"""
        text_norm = self._normalize_text(text)
        
        # 1. Exact name match
        for rid, cons in self.consumables.items():
            candidates = [cons.name]
            if rid in self.consumable_primary_alias:
                candidates.append(self.consumable_primary_alias[rid])
            for c in candidates:
                if self._normalize_text(c) == text_norm:
                    return rid
        
        # 2. Name contains match
        for rid, cons in self.consumables.items():
            candidates = [cons.name]
            if rid in self.consumable_primary_alias:
                candidates.append(self.consumable_primary_alias[rid])
            for c in candidates:
                c_norm = self._normalize_text(c)
                if c_norm and (c_norm in text_norm or text_norm in c_norm):
                    return rid
        
        # 3. Type match
        for rid, cons in self.consumables.items():
            c_type = self._normalize_text(cons.type)
            if c_type and (c_type in text_norm or text_norm in c_type):
                return rid
        
        # 4. Keyword rule matching
        keyword_rules = {
            "96-well": ["96-well", "microplate", "well plate"],
            "384-well": ["384-well", "microplate", "well plate"],
            "pcr": ["pcr", "pcr plate", "pcr tube"],
            "tip": ["pipette tip", "tip"],
            "reservoir": ["reservoir", "reagent reservoir"],
            "zh_96": ["96孔", "96 孔"],
            "zh_384": ["384孔", "384 孔"],
            "zh_tip": ["吸头"],
            "zh_reservoir": ["试剂槽", "储液槽"],
            "zh_plate": ["孔板", "酶标板", "深孔板", "浅孔板"],
        }
        
        for rule_key, rule_keywords in keyword_rules.items():
            if any(self._normalize_text(rk) in text_norm or rk in text for rk in rule_keywords):
                for rid, cons in self.consumables.items():
                    alias_blob = " ".join(self.consumable_alias_texts.get(rid, []))
                    cons_text = f"{cons.name} {cons.type} {alias_blob}"
                    cons_norm = self._normalize_text(cons_text)
                    if any(self._normalize_text(rk) in cons_norm or rk in cons_text for rk in rule_keywords):
                        return rid
        
        return None
    
    # ==================== Generation Interfaces ====================
    
    def get_simplified_instruments(self) -> str:
        """Generate simplified instrument list"""
        lines = ["## Available Instruments (resourceId must be selected from this list)"]
        lines.append("| ID | Name | Usage Keywords |")
        lines.append("|-----|------|-----------|")
        
        for rid, inst in self.instruments.items():
            keywords = ", ".join(inst.keywords[:5])  
            lines.append(f"| {rid} | {inst.name} | {keywords} |")
        
        return '\n'.join(lines)
    
    def get_simplified_consumables(self) -> str:
        """Generate simplified consumable list"""
        lines = ["## Available Consumables (consumableId selected from this list)"]
        lines.append("| ID | Name | Type |")
        lines.append("|-----|------|------|")
        
        for rid, cons in self.consumables.items():
            lines.append(f"| {rid} | {cons.name} | {cons.type} |")
        
        return '\n'.join(lines)
    
    def get_instrument_by_id(self, rid: int) -> Optional[Instrument]:
        return self.instruments.get(rid)
    
    def get_consumable_by_id(self, rid: int) -> Optional[Consumable]:
        return self.consumables.get(rid)

    # Unified registry protocol methods
    def get_by_id(self, rid: int) -> Optional[Instrument]:
        return self.get_instrument_by_id(rid)

    def list_available(self) -> List[Instrument]:
        return list(self.instruments.values())

    def is_valid_id(self, rid: int) -> bool:
        return self.is_valid_instrument_id(rid) or self.is_valid_consumable_id(rid)

    def summary_for_prompt(self) -> str:
        return self.get_simplified_instruments()


resource_registry = ResourceRegistry()
