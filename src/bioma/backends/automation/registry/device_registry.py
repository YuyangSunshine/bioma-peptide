# src/capabilities/registry/device_registry.py

"""
Device Registry - Dynamically generates Prompt content from instruments.csv
Ensures the Prompt stays in sync with actual device configurations
"""

import csv
import json
from typing import Dict, List, Optional
from pathlib import Path
from bioma.backends.automation import settings
from bioma.backends.automation.registry.base import BaseRegistry


class DeviceRegistry(BaseRegistry):
    """Device Registry - Manages device information and generates Prompts"""
    
    def __init__(self, instruments_path: str = str(settings.INSTRUMENTS_PATH)):
        self.instruments_path = Path(instruments_path)
        self.devices: Dict[int, Dict] = {}
        self._en_name_map: Dict[int, str] = {}
        self._load_english_aliases()
        self._load_devices()

    def _load_english_aliases(self):
        """Load optional English name aliases from data/instruments_en.json."""
        p = Path(settings.INSTRUMENTS_EN_PATH)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for row in data:
                    try:
                        rid = int(row.get("resourceId", 0))
                    except Exception:
                        continue
                    name = str(row.get("deviceName", "")).strip()
                    if rid and name:
                        self._en_name_map[rid] = name
        except Exception:
            return
    
    def _load_devices(self):
        """Load device information from CSV"""
        if not self.instruments_path.exists():
            print(f"[Warning] Device CSV does not exist: {self.instruments_path}; loading {settings.INSTRUMENTS_EN_PATH}")
            self._load_devices_from_json()
            return
        
        with open(self.instruments_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rid = int(row.get('resourceId', 0))
                    status = row.get('statusCode', '').strip().lower()
                    display_name = self._en_name_map.get(rid, row.get('deviceName', ''))
                    
                    self.devices[rid] = {
                        'resourceId': rid,
                        'deviceName': display_name,
                        'model': row.get('model', ''),
                        'status': status,
                        'description': row.get('description', ''),
                        'specifications': row.get('specifications', ''),
                        'parameters': self._parse_json(row.get('parameters', '{}')),
                        'template': self._parse_json(row.get('template', '{}')),
                    }
                except (ValueError, KeyError) as e:
                    print(f"[Warning] Failed to parse device row: {e}")

    def _load_devices_from_json(self):
        """Load device information from the bundled English JSON fallback."""
        p = Path(settings.INSTRUMENTS_EN_PATH)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[Warning] Failed to load device JSON fallback: {e}")
            return

        if not isinstance(data, list):
            return

        for row in data:
            try:
                rid = int(row.get('resourceId', 0))
                status = str(row.get('statusCode', '')).strip().lower()
                display_name = self._en_name_map.get(rid, row.get('deviceName', ''))

                self.devices[rid] = {
                    'resourceId': rid,
                    'deviceName': display_name,
                    'model': row.get('model', ''),
                    'status': status,
                    'description': row.get('description', ''),
                    'specifications': row.get('specifications', ''),
                    'parameters': self._parse_json(row.get('parameters', '{}')),
                    'template': self._parse_json(row.get('template', '{}')),
                }
            except (ValueError, KeyError) as e:
                print(f"[Warning] Failed to parse device JSON row: {e}")
    
    def _parse_json(self, json_str: str) -> Dict:
        """Safely parse JSON"""
        if not json_str or json_str == '{}':
            return {}
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return {}
    
    def get_available_devices(self) -> List[Dict]:
        """Get all available devices"""
        return [d for d in self.devices.values() if d['status'] == 'available']
    
    def get_device_by_id(self, rid: int) -> Optional[Dict]:
        """Get device by ID"""
        return self.devices.get(rid)

    # Unified registry protocol methods
    def get_by_id(self, rid: int) -> Optional[Dict]:
        return self.get_device_by_id(rid)

    def list_available(self) -> List[Dict]:
        return self.get_available_devices()

    def is_valid_id(self, rid: int) -> bool:
        return rid in self.devices

    def summary_for_prompt(self) -> str:
        return self.generate_device_table_for_prompt()
    
    def get_device_type(self, rid: int) -> str:
        """Get device type"""
        device = self.devices.get(rid)
        if not device:
            return "unknown"

        name = device['deviceName'].lower()

        if 'pipetting' in name or 'biomek' in name.lower():
            return "pipetting_station"
        elif 'qpcr' in name or ('fluorescence' in name and 'pcr' in name):
            return "qpcr"
        elif 'pcr' in name:
            return "pcr"
        elif 'sealing' in name:
            return "sealer"
        elif 'peeling' in name:
            return "peeler"
        elif 'centrifugation' in name or 'centrifuge' in name:
            return "centrifuge"
        elif 'incubation' in name or 'cultivation' in name:
            return "incubator"
        elif 'microplate assay' in name:
            return "plate_reader"
        elif 'plate washing' in name:
            return "washer"
        elif 'refrigerator' in name or 'cold storage' in name:
            return "refrigerator"
        elif 'electrophoresis' in name:
            return "capillary_electrophoresis"
        elif 'cloning' in name or 'selection' in name:
            return "colony_picker"
        else:
            return "unknown"
    
    def generate_device_table_for_prompt(self) -> str:
        """Generate device table for Prompt"""
        available = self.get_available_devices()
        
        lines = [
            "### 🔧 Automation Device ID Table (Complete)",
            "",
            "| resourceId | Device Name | Device Type | Core Capabilities | keyParams Critical Parameters |",
            "|------------|-------------|-------------|-------------------|-------------------------------|",
        ]

        # Mapping from device type to key parameters
        key_params_map = {
            "pipetting_station": "transfers[], mixAfter, consumable",
            "pcr": "cycleCount, annealTemp, extendTime, consumable",
            "qpcr": "cycleCount, annealTemp, channel, consumable",
            "sealer": "temperature, consumable",
            "peeler": "consumable",
            "centrifuge": "force_g, duration, temperature",
            "incubator": "temperature, duration, shakeSpeed",
            "plate_reader": "mode, wavelength, consumable",
            "washer": "cycles, volume, consumable",
            "refrigerator": "temperature, duration, consumable",
            "capillary_electrophoresis": "sampleCount, consumable",
            "colony_picker": "colonyCount, consumable, targetConsumable",
        }

        # Mapping from device type to capability description
        capability_map = {
            "pipetting_station": "Liquid transfer, shaking, temperature control, magnetic bead separation",
            "pcr": "PCR amplification, constant temperature incubation",
            "qpcr": "Real-time fluorescent PCR, melting curve",
            "sealer": "Heat sealing",
            "peeler": "Automatic film peeling",
            "centrifuge": "Centrifugal separation (500-15000×g)",
            "incubator": "Constant temperature shaking culture",
            "plate_reader": "Absorbance/fluorescence/luminescence detection",
            "washer": "Microplate cleaning",
            "refrigerator": "Low-temperature storage (4°C)",
            "capillary_electrophoresis": "Fragment analysis",
            "colony_picker": "Colony picking",
        }
        
        for device in sorted(available, key=lambda x: x['resourceId']):
            rid = device['resourceId']
            name = device['deviceName']
            dtype = self.get_device_type(rid)
            capability = capability_map.get(dtype, device.get('description', '')[:30])
            key_params = key_params_map.get(dtype, "consumable")
            
            lines.append(f"| **{rid}** | {name} | {dtype} | {capability} | {key_params} |")
        
        # Add special notes
        lines.extend([
            "",
            "### Device Selection Guide",
            "- **Pipetting Station**: 59 (i7, more full-featured) takes priority over 48 (i5)",
            "- **PCR Instrument**: 57 and 69 have the same function; choose either",
            "- **Sealing/Peeling**: Must seal before putting into PCR instrument or incubator; peel after operation",
            "- **External/Manual**: Set resourceId = -1 and configure operationType",
        ])
        
        return "\n".join(lines)
    
    def generate_device_params_guide(self) -> str:
        """Generate device parameter guide"""
        lines = [
            "## 📋 Detailed Explanation of keyParams for Each Device",
            "",
        ]
        
        # Detailed parameter explanation
        param_details = {
            "pipetting_station (59/48)": """
```json
{
"transfers": [
  {"reagent": "Reagent name", "volume": 10, "source": "Source plate", "target": "Target plate"}
],
"mixAfter": true,  // Whether to mix after pipetting
"consumable": "Name of the main plate for operation"
}
```""",
            "pcr (57/69)": """
```json
{
    "consumable": "PCR plate",
    "cycleCount": 30,        // Number of cycles
    "annealTemp": 55,        // Annealing temperature (°C)
    "annealTime": 30,        // Annealing time (seconds)
    "extendTime": 60,        // Extension time (seconds)
    "denatureTime": 30       // Denaturation time (seconds, optional)
}
```""",
            "centrifuge (66)": """
```json
{
    "consumable": "Centrifuge plate",
    "force_g": 3000,        // Centrifugal force (×g)
    "duration": 300,        // Time (seconds)
    "temperature": 25       // Temperature (°C)
}
```""",
            "incubator (65)": """
```json
{
    "consumable": "Culture plate",
    "temperature": 37,      // Temperature (°C)
    "duration": 3600,       // Time (seconds)
    "shakeSpeed": 200       // Shaking speed (rpm)
}
```""",
            "plate_reader (55)": """
```json
{
    "consumable": "Detection plate",
    "mode": "absorbance",   // absorbance/fluorescence/luminescence
    "wavelength": 450,      // Detection wavelength (nm)
    "excitation_nm": 494,   // Excitation wavelength (for fluorescence mode)
    "emission_nm": 518      // Emission wavelength (for fluorescence mode)
}
```""",
            "sealer (67) / peeler (50)": """
```json
{
  "consumable": "Plate to be sealed/peeled"
}
```""",
        }
        
        for device_type, params in param_details.items():
            lines.append(f"### {device_type}")
            lines.append(params)
            lines.append("")
        
        return "\n".join(lines)


device_registry = DeviceRegistry()


def get_device_prompt_content() -> str:
    """Get the complete device Prompt content"""
    return (
        device_registry.generate_device_table_for_prompt() + 
        "\n\n" + 
        device_registry.generate_device_params_guide()
    )
