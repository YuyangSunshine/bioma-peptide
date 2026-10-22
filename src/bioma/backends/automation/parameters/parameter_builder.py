# src/capabilities/automation/parameter_builder.py

"""
Parameter Builder - Convert LLM's Concise Output to Complete Machine-Executable Code

Design Principles:
1. LLM is only responsible for "decision-making": select equipment, set key parameters, determine sequence
2. Python code is responsible for "filling": build the complete parameter structure based on the device Schema
3. Strictly follow the target format to ensure machine executability
"""

import json
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from copy import deepcopy
from bioma.backends.automation import settings
from bioma.backends.automation.registry.resource_registry import ResourceRegistry


@dataclass
class ConsumableInstance:
    """Consumable Instance"""
    instance_id: str
    consumable_id: Optional[int]
    consumable_name: str
    source_node_id: str
    used_by_node_ids: List[str] = field(default_factory=list)

class ParameterBuilder():
    """
    Parameter Builder
    """
    
    def __init__(self, resource_registry=None, instruments_path: str = str(settings.INSTRUMENTS_PATH)):
        self.resource_registry = resource_registry
        self.consumable_instances: Dict[str, ConsumableInstance] = {}
        self.node_id_counter = 0
        
        # Load device information from CSV
        self._device_info = {}
        self._device_templates = {}
        self._load_from_csv(instruments_path)
        
        # Default parameter template (as fallback)
        self._default_templates = self._build_default_templates()
    
    def _load_from_csv(self, path: str):
        """Load device information and parameter templates from CSV"""
        import csv
        from pathlib import Path
        
        csv_path = Path(path)
        if not csv_path.exists():
            print(f"[Warning] Device CSV does not exist: {path}, using built-in configuration")
            self._device_info = self._build_device_info_fallback()
            return
        
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rid = int(row.get('resourceId', 0))
                    status = row.get('statusCode', '').strip().lower()
                    
                    # Only load available devices
                    if status != 'available':
                        continue
                    
                    device_name = row.get('deviceName', '')
                    model = row.get('model', '')
                    
                    device_type = self._infer_device_type(device_name)
                    
                    self._device_info[rid] = {
                        'name': model,
                        'fullName': f"{model} / {device_name}",
                        'type': device_type,
                        'description': row.get('description', ''),
                    }
                    
                    params_str = row.get('parameters', '{}')
                    if params_str and params_str != '{}':
                        try:
                            self._device_templates[rid] = json.loads(params_str)
                        except json.JSONDecodeError:
                            pass
                            
                except Exception as e:
                    print(f"[Warning] Failed to parse device {row.get('resourceId')}: {e}")
    
    def _infer_device_type(self, name: str) -> str:
        """Infer device type from device name"""
        name_lower = name.lower()
        
        type_patterns = [
            (["pipette", "biomek", "pipett"], "pipetting_station"),
            (["fluorescence", "qpcr", "lightcycler"], "qpcr"),
            (["pcr machine", "pcr", "atc"], "pcr"),
            (["seal", "a4s"], "sealer"),
            (["peel", "xp-a"], "peeler"),
            (["centrifuge", "rotanta"], "centrifuge"),
            (["incubate", "cytomat 2c450"], "incubator"),
            (["microplate reader", "plate reader", "spectramax"], "plate_reader"),
            (["plate washer", "wash", "405"], "washer"),
            (["refrigerator", "refrigerate", "cytomat 2c425"], "refrigerator"),
            (["electrophoresis", "labchip"], "capillary_electrophoresis"),
            (["colony", "pick", "k8"], "colony_picker"),
        ]
        
        for patterns, dtype in type_patterns:
            for pattern in patterns:
                if pattern in name_lower:
                    return dtype
        
        return "unknown"
    
    def _build_device_info_fallback(self) -> Dict[int, Dict]:
        """Built-in device information (fallback)"""
        return {
            48: {"name": "Biomek i5", "fullName": "Biomek i5 / Automated Pipettor Station-2", "type": "pipetting_station"},
            50: {"name": "XP-A_230V", "fullName": "XP-A_230V / Automated Film Peeler", "type": "peeler"},
            54: {"name": "405LSRS-SN", "fullName": "405LSRS-SN / Plate Washer", "type": "washer"},
            55: {"name": "SpectraMax i3x", "fullName": "SpectraMax i3x / Multifunctional Microplate Reader", "type": "plate_reader"},
            56: {"name": "LightCycler480 II", "fullName": "LightCycler480 II / Fluorescence Quantitative PCR Instrument", "type": "qpcr"},
            57: {"name": "ATC 96 well Block", "fullName": "ATC 96 well Block / Automated PCR Instrument", "type": "pcr"},
            59: {"name": "Biomek i7 Hybrid", "fullName": "Biomek i7 Hybrid (Span8+MC96) / Automated Pipettor Station-1", "type": "pipetting_station"},
            60: {"name": "LabChip GX Touch HT", "fullName": "LabChip GX Touch HT / Capillary Electrophoresis Instrument", "type": "capillary_electrophoresis"},
            61: {"name": "Cytomat 2C425", "fullName": "Cytomat 2C425 / Automated Refrigerator", "type": "refrigerator"},
            63: {"name": "K8", "fullName": "K8 / Microbial Colony Picker", "type": "colony_picker"},
            65: {"name": "Cytomat 2C450-LIN", "fullName": "Cytomat 2C450-LIN / Automated Incubator", "type": "incubator"},
            66: {"name": "Rotanta 460 Robotic", "fullName": "Rotanta 460 Robotic / Automated Centrifuge", "type": "centrifuge"},
            67: {"name": "A4S (4ti-0665)", "fullName": "A4S (4ti-0665) / Automated Sealer", "type": "sealer"},
            69: {"name": "ATC 96 well Block", "fullName": "ATC 96 well Block / Automated PCR Instrument-2", "type": "pcr"},
        }
    
    def _build_default_templates(self) -> Dict[str, Dict]:
        """build default templates"""
        return {
            "pipetting_station": {
                "workflowSteps": []
            },
            "pipetting_step_transfer": {
                "stepName": "transfer",
                "operationType": "transfer",
                "reagentName": "",
                "sourceConsumable": "",
                "targetConsumable": "",
                "volume": 100,
                "unit": "µL",
                "shakingMode": "clockwise",
                "rotationSpeed_rpm": 300,
                "shakingDuration": "00:02:30",
                "temperature_C": 25,
                "temperatureDuration": "00:10:00",
                "magneticConsumable": "",
                "magneticDuration": "00:02:30"
            },
            "pipetting_step_shake": {
                "stepName": "shake",
                "operationType": "shake",
                "reagentName": "",
                "sourceConsumable": "",
                "targetConsumable": "",
                "volume": 100,
                "unit": "µL",
                "shakingMode": "clockwise",
                "rotationSpeed_rpm": 300,
                "shakingDuration": "00:02:00",
                "temperature_C": 25,
                "temperatureDuration": "00:10:00",
                "magneticConsumable": "",
                "magneticDuration": "00:02:30"
            },
            "pipetting_step_temperature": {
                "stepName": "temperature",
                "operationType": "temperature",
                "reagentName": "",
                "sourceConsumable": "",
                "targetConsumable": "",
                "volume": 100,
                "unit": "µL",
                "shakingMode": "clockwise",
                "rotationSpeed_rpm": 300,
                "shakingDuration": "00:02:30",
                "temperature_C": 37,
                "temperatureDuration": "00:30:00",
                "magneticConsumable": "",
                "magneticDuration": "00:02:30"
            },
            "pipetting_step_magnetic": {
                "stepName": "magnetic",
                "operationType": "magneticModule",
                "reagentName": "",
                "sourceConsumable": "",
                "targetConsumable": "",
                "volume": 100,
                "unit": "µL",
                "shakingMode": "clockwise",
                "rotationSpeed_rpm": 300,
                "shakingDuration": "00:02:30",
                "temperature_C": 25,
                "temperatureDuration": "00:10:00",
                "magneticConsumable": "",
                "magneticDuration": "00:05:00"
            },
            "pcr": {
                "consumableName": "",
                "reactionVolume_uL": 50,
                "lidTemperature_C": 105,
                "programSteps": [
                    {"stepType": "temperature", "stepNumber": 1, "stepName": "Initial Denaturation", "temperature_C": 95, "duration": "00:05:00"},
                    {"stepType": "temperature", "stepNumber": 2, "stepName": "Denaturation", "temperature_C": 95, "duration": "00:00:30"},
                    {"stepType": "temperature", "stepNumber": 3, "stepName": "Annealing", "temperature_C": 55, "duration": "00:00:30"},
                    {"stepType": "temperature", "stepNumber": 4, "stepName": "Extension", "temperature_C": 72, "duration": "00:01:00"},
                    {"stepType": "cycle", "stepNumber": 5, "cycleStartStep": 2, "cycleEndStep": 4, "cycleCount": 30},
                    {"stepType": "temperature", "stepNumber": 6, "stepName": "Final Extension", "temperature_C": 72, "duration": "00:10:00"},
                    {"stepType": "temperature", "stepNumber": 7, "stepName": "Hold", "temperature_C": 4, "duration": "99:99:99"}
                ]
            },
            "qpcr": {
                "consumableName": "",
                "analysisMode": "quantification",
                "detectionChannel": "FAM",
                "temperatureProgram": []
            },
            "sealer": {
                "consumableName": "",
                "temperature_C": 170,
                "duration": 3,
                "comment": ""
            },
            "peeler": {
                "consumableName": "",
                "duration": 30,
                "peelingSpeed": "medium",
                "comment": ""
            },
            "incubator": {
                "consumableName": "",
                "temperature_C": 37,
                "rotationSpeed_rpm": 200,
                "incubationTime": "01:00:00"
            },
            "centrifuge": {
                "consumableName": "",
                "centrifugalForce_g": 3000,
                "centrifugalTime": "00:05:00",
                "temperature_C": 25,
                "brake": "medium"
            },
            "plate_reader": {
                "consumableName": "",
                "plateType": "96_clear",
                "detectionMode": "absorbance",
                "volume_uL": 200,
                "emission_nm": 518,
                "excitation_nm": 494,
                "detectionWavelength": 450,
                "enableShaking": False,
                "executionDuration": "00:05:00",
                "shakingConfig": {
                    "shakingMode": "orbital",
                    "shakingDuration": 10,
                    "shakingIntensity": "medium"
                }
            },
            "washer": {
                "consumableName": "",
                "cycleCount": 3,
                "enableShaking": True,
                "shakingConfig": {
                    "shakingDuration": 10,
                    "shakingIntensity": "medium"
                },
                "aspirationConfig": {
                    "aspirationMode": "top_aspiration",
                    "movementSpeed": "medium"
                },
                "dispensingConfig": {
                    "dispensingFlowRate": "medium",
                    "dispensingVolume_uL": 300
                }
            },
            "refrigerator": {
                "consumableName": "",
                "storageTemperature_C": 4,
                "storageTime": "01:00:00"
            },
            "capillary_electrophoresis": {
                "consumableName": "",
                "sampleCount": 96,
                "sampleVolume_uL": 5,
                "sourceWellPosition": "A1"
            },
            "colony_picker": {
                "consumableName": "",
                "targetConsumable": "",
                "colonyCount": 96,
                "needle_depth_mm": 1.0,
                "exposure_time_ms": 100,
                "comment": ""
            }
        }
   
    def _build_full_node(self, simplified_node: Dict, full_node_id: int) -> Dict:
        """Build complete node structure - prioritize using CSV templates"""
        resource_id = simplified_node.get("resourceId")
        action = simplified_node.get("action", "")
        key_params = simplified_node.get("keyParams", {}) or simplified_node.get("parameters", {})

        # Get device information
        device_info = self._device_info.get(resource_id, {})
        device_type = device_info.get("type", "unknown")

        # External/manual steps
        if resource_id == -1:
            return self._build_external_node(simplified_node, full_node_id, key_params)

        # Basic node structure
        node = {
            "templateNodeId": full_node_id,
            "resourceId": resource_id,
            "nodeName": device_info.get("fullName", f"Device_{resource_id}"),
            "nodeType": "device",
            "parameters": {}
        }

        str_node_id = str(full_node_id)

        # Prioritize using templates from CSV
        if resource_id in self._device_templates:
            base_params = deepcopy(self._device_templates[resource_id])
            # Override with keyParams
            node["parameters"] = self._merge_params(base_params, key_params, str_node_id)
        else:
            # Use built-in constructor
            node["parameters"] = self._build_params_by_type(device_type, key_params, str_node_id, action)

        return node
    
    def _build_external_node(self, simplified_node: Dict, full_node_id: int, key_params: Dict) -> Dict:
        """Build external/manual node"""
        operation_type = key_params.get("operationType", "manual")
        device_name = key_params.get("deviceName", "Manual Operation" if operation_type == "manual" else "External Device")
        description = key_params.get("description", key_params.get("notes", ""))

        return {
            "templateNodeId": full_node_id,
            "resourceId": -1,
            "nodeName": f"[{ 'EXTERNAL' if operation_type == 'external' else 'MANUAL' }] {device_name}",
            "nodeType": "external" if operation_type == "external" else "manual",
            "parameters": {
                "operationType": operation_type,
                "deviceName": device_name,
                "description": description,
                "notes": f"[{ 'EXTERNAL' if operation_type == 'external' else 'MANUAL' }] {device_name}: {description}",
                **{k: v for k, v in key_params.items()
                if k not in ["operationType", "deviceName", "description", "notes"]}
            }
        }
    
    def _merge_params(self, base: Dict, overrides: Dict, node_id: str) -> Dict:
        """Merge parameters, using overrides to overwrite base"""
        result = deepcopy(base)
        
        param_mapping = {
            "cycleCount": ["cycleCount", "cycles", "cycle_count"],
            "annealTemp": ["annealTemp", "annealing_temp", "anneal_temperature"],
            "temperature_C": ["temperature_C", "temperature", "temp"],
            "duration": ["duration", "time", "duration_s"],
            "consumable": ["consumable", "consumableName", "plate"],
            "force_g": ["force_g", "centrifugalForce_g", "g_force"],
        }
        
        for target_key, source_keys in param_mapping.items():
            for src_key in source_keys:
                if src_key in overrides and overrides[src_key] is not None:
                    result[target_key] = overrides[src_key]
                    break
        
        consumable_name = None
        for key in ["consumable", "consumableName", "plate"]:
            if key in overrides and overrides[key]:
                consumable_name = overrides[key]
                break
        
        if consumable_name:
            result["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id)
        
        return result
    
    def _generate_node_id(self) -> int:
        """Generate Node ID"""
        base = int(time.time() * 1000000)
        self.node_id_counter += 1
        return base + self.node_id_counter
    
    def _generate_consumable_instance_id(self) -> str:
        """Generate Consumable ID"""
        return str(int(time.time() * 1000000) + len(self.consumable_instances))
    
    def _get_or_create_consumable(
        self, 
        name: str, 
        source_node_id: str,
        consumable_id: Optional[int] = None
    ) -> str:
        """Get or create a consumable instance, return the instance name"""

        if name in self.consumable_instances:
            inst = self.consumable_instances[name]
            if source_node_id not in inst.used_by_node_ids:
                inst.used_by_node_ids.append(source_node_id)
            return name
        
        instance = ConsumableInstance(
            instance_id=self._generate_consumable_instance_id(),
            consumable_id=consumable_id,
            consumable_name=name,
            source_node_id=source_node_id,
            used_by_node_ids=[source_node_id]
        )
        self.consumable_instances[name] = instance
        return name
  
    def _match_consumable_id(self, name: str) -> Optional[int]:
        """Match consumable ID by name"""
        if self.resource_registry:
            return self.resource_registry.match_consumable_by_text(name)

        # Simple rule matching
        name_lower = name.lower()
        if 'pcr' in name_lower and '96' in name_lower:
            return 10036
        if 'deep well' in name_lower or 'deep' in name_lower:
            if '2.2' in name_lower:
                return 10038
            return 10037
        if 'reservoir' in name_lower:
            return 10035
        if 'elisa' in name_lower:
            if 'black' in name_lower:
                return 10040
            if 'white' in name_lower:
                return 10039
            return 10041
        if 'pipette tip' in name_lower or 'tip' in name_lower:
            return 10025

        return None
    
    def _seconds_to_hms(self, seconds) -> str:
        """Convert seconds to HH:MM:SS format"""
        if isinstance(seconds, str):
            if ':' in seconds:
                return seconds
            try:
                seconds = int(float(seconds))
            except (TypeError, ValueError):
                return "00:01:00"
        
        if not isinstance(seconds, (int, float)):
            return "00:01:00"
        
        seconds = int(seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    
    
    def _build_params_by_type(self, device_type: str, key_params: Dict, node_id: str, action: str = "") -> Dict:
        """Build parameters based on device type (fallback method)"""
        # Restore the original parameter construction methods for each device
        builder_map = {
            "pipetting_station": self._build_pipetting_params,
            "pcr": self._build_pcr_params,
            "qpcr": self._build_qpcr_params,
            "sealer": self._build_sealer_params,
            "peeler": self._build_peeler_params,
            "incubator": self._build_incubator_params,
            "centrifuge": self._build_centrifuge_params,
            "plate_reader": self._build_reader_params,
            "washer": self._build_washer_params,
            "refrigerator": self._build_refrigerator_params,
            "capillary_electrophoresis": self._build_ce_params,
            "colony_picker": self._build_colony_picker_params,
        }
        
        builder = builder_map.get(device_type, self._build_generic_params)
        
        if device_type == "pipetting_station":
            return builder(key_params, node_id, action)
        else:
            return builder(key_params, node_id)

    def build_experiment_flow(self, simplified_flow: Dict) -> Dict:
        """
        Convert the simplified flow generated by LLM into a complete machine-executable code
        
        Args:
            simplified_flow: The simplified structure generated by LLM, formatted as follows:
            {
                "templateName": "Experiment Name",
                "description": "Description",
                "nodes": [
                    {"templateNodeId": 1, "resourceId": 59, "action": "Pipetting", "keyParams": {...}},
                    ...
                ],
                "connections": [
                    {"sourceNodeId": 1, "targetNodeId": 2},
                    ...
                ]
            }
        
        Returns:
            Complete machine-executable JSON
        """
        # Reset state
        self.consumable_instances = {}
        self.node_id_counter = 0
        
        # Build node ID mapping (simple ID -> full ID)
        node_id_map: Dict[int, int] = {}
        
        result = {
            "templateName": simplified_flow.get("templateName", "Experiment Flow"),
            "description": simplified_flow.get("description", ""),
            "nodes": [],
            "connections": [],
            "consumables": []
        }
        
        # Build Nodes
        for node in simplified_flow.get("nodes", []):
            simple_id = node.get("templateNodeId")
            full_id = self._generate_node_id()
            node_id_map[simple_id] = full_id
            
            full_node = self._build_full_node(node, full_id)
            result["nodes"].append(full_node)
        
        # Build Connections
        for conn in simplified_flow.get("connections", []):
            source_simple = conn.get("sourceNodeId")
            target_simple = conn.get("targetNodeId")
            
            result["connections"].append({
                "sourceNodeId": node_id_map.get(source_simple, source_simple),
                "targetNodeId": node_id_map.get(target_simple, target_simple)
            })
        
        # Build Consumables
        result["consumables"] = self._build_consumables_list()
        
        return result
    
    # ==================== Device-Specific Parameter Builder ====================
    
    def _build_pipetting_params(self, key_params: Dict, node_id: str, action: str = "") -> Dict:
        """Build pipetting workstation parameters"""
        workflow_steps = []
        
        # Case 1: LLM directly provides workflowSteps
        if "workflowSteps" in key_params:
            for step in key_params["workflowSteps"]:
                full_step = self._build_pipetting_step(step, node_id)
                workflow_steps.append(full_step)
        
        # Case 2: LLM provides a list of transfers
        elif "transfers" in key_params:
            for i, transfer in enumerate(key_params["transfers"]):
                step = {
                    "stepName": transfer.get("stepName", f"transfer_{i+1}"),
                    "operationType": "transfer",
                    "reagentName": transfer.get("reagent", transfer.get("reagentName", "")),
                    "sourceConsumable": transfer.get("source", transfer.get("sourceConsumable", "")),
                    "targetConsumable": transfer.get("target", transfer.get("targetConsumable", "")),
                    "volume": transfer.get("volume", 100),
                    "unit": transfer.get("unit", "µL")
                }
                full_step = self._build_pipetting_step(step, node_id)
                workflow_steps.append(full_step)
        
        # Case 3: Single operation (determine type based on action)
        elif action:
            action_lower = action.lower()
            if "transfer" in action_lower:
                step = {"operationType": "transfer", **key_params}
            elif "shake" in action_lower or "mix" in action_lower:
                step = {"operationType": "shake", **key_params}
            elif "hold" in action  or "temperature" in action_lower:
                step = {"operationType": "temperature", **key_params}
            elif "magnetic" in action_lower:
                step = {"operationType": "magneticModule", **key_params}
            else:
                step = {"operationType": "transfer", **key_params}
            
            full_step = self._build_pipetting_step(step, node_id)
            workflow_steps.append(full_step)
        
        # Case 4: No explicit information, create empty step
        else:
            step = {"operationType": "transfer", **key_params}
            full_step = self._build_pipetting_step(step, node_id)
            workflow_steps.append(full_step)
        
        return {"workflowSteps": workflow_steps}
    
    def _build_pipetting_step(self, step_params: Dict, node_id: str) -> Dict:
        """Build a single pipetting step (populate all fields)"""
        op_type = step_params.get("operationType", "transfer")
        
        # Select the corresponding template
        template_key = f"pipetting_step_{op_type}"
        if template_key not in self._default_templates:
            template_key = "pipetting_step_transfer"
        
        # Copy the template
        full_step = deepcopy(self._default_templates[template_key])
        
        # Override with provided parameters
        param_mapping = {
            "stepName": "stepName",
            "step_name": "stepName",
            "operationType": "operationType",
            "operation_type": "operationType",
            "reagentName": "reagentName",
            "reagent": "reagentName",
            "reagent_name": "reagentName",
            "sourceConsumable": "sourceConsumable",
            "source": "sourceConsumable",
            "source_consumable": "sourceConsumable",
            "targetConsumable": "targetConsumable",
            "target": "targetConsumable",
            "target_consumable": "targetConsumable",
            "volume": "volume",
            "vol": "volume",
            "unit": "unit",
            "shakingMode": "shakingMode",
            "shaking_mode": "shakingMode",
            "rotationSpeed_rpm": "rotationSpeed_rpm",
            "speed": "rotationSpeed_rpm",
            "rotation_speed": "rotationSpeed_rpm",
            "shakingDuration": "shakingDuration",
            "shake_duration": "shakingDuration",
            "shake_time": "shakingDuration",
            "temperature_C": "temperature_C",
            "temperature": "temperature_C",
            "temp": "temperature_C",
            "temperatureDuration": "temperatureDuration",
            "temp_duration": "temperatureDuration",
            "magneticConsumable": "magneticConsumable",
            "magnetic_consumable": "magneticConsumable",
            "magneticDuration": "magneticDuration",
            "magnetic_duration": "magneticDuration",
        }
        
        for src_key, dest_key in param_mapping.items():
            if src_key in step_params and step_params[src_key] is not None:
                value = step_params[src_key]
                
                if dest_key in ["shakingDuration", "temperatureDuration", "magneticDuration"]:
                    value = self._seconds_to_hms(value)
                
                full_step[dest_key] = value
        
        if full_step.get("sourceConsumable"):
            self._get_or_create_consumable(
                full_step["sourceConsumable"],
                node_id,
                self._match_consumable_id(full_step["sourceConsumable"])
            )
        if full_step.get("targetConsumable"):
            self._get_or_create_consumable(
                full_step["targetConsumable"],
                node_id,
                self._match_consumable_id(full_step["targetConsumable"])
            )
        
        return full_step
    
    def _build_pcr_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build pcr params"""
        params = deepcopy(self._default_templates["pcr"])
        
        # Handle consumables
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))


        # Override basic parameters
        reaction_volume = key_params.get(
            "reactionVolume_uL",
            key_params.get("reactionVolume_ul", key_params.get("volume")),
        )
        if reaction_volume is not None:
            params["reactionVolume_uL"] = reaction_volume


        if "lidTemperature_C" in key_params or "lidTemp" in key_params:
            params["lidTemperature_C"] = key_params.get("lidTemperature_C", key_params.get("lidTemp", 105))


        # Handle PCR program
        if "programSteps" in key_params:
            params["programSteps"] = key_params["programSteps"]
        else:
            # Update default program based on key_params
            cycle_count = key_params.get("cycleCount", key_params.get("cycles", 30))
            anneal_temp = key_params.get("annealTemp", key_params.get("annealing_temp", 55))
            anneal_time = key_params.get("annealTime", key_params.get("annealing_time", 30))
            extend_time = key_params.get("extendTime", key_params.get("extension_time", 60))
            denature_time = key_params.get("denatureTime", key_params.get("denaturation_time", 30))


            # Update program steps
            for step in params["programSteps"]:
                if step.get("stepType") == "cycle":
                    step["cycleCount"] = cycle_count
                elif step.get("stepName") == "Annealing":
                    step["temperature_C"] = anneal_temp
                    step["duration"] = self._seconds_to_hms(anneal_time)
                elif step.get("stepName") == "Extension":
                    step["duration"] = self._seconds_to_hms(extend_time)
                elif step.get("stepName") == "Denaturation":
                    step["duration"] = self._seconds_to_hms(denature_time)


        return params
    
    def _build_qpcr_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build qpcr params"""
        params = deepcopy(self._default_templates["qpcr"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", ""))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        params["analysisMode"] = key_params.get("analysisMode", "quantification")
        params["detectionChannel"] = key_params.get("detectionChannel", key_params.get("channel", "FAM"))
        
        if "temperatureProgram" in key_params:
            params["temperatureProgram"] = key_params["temperatureProgram"]
        
        return params
    
    def _build_sealer_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build sealer params"""
        params = deepcopy(self._default_templates["sealer"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        params["temperature_C"] = key_params.get("temperature_C", key_params.get("temperature", key_params.get("temp", 170)))
        params["duration"] = key_params.get("duration", key_params.get("time", 3))
        params["comment"] = key_params.get("comment", "")
        
        return params
    
    def _build_peeler_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build peeler params"""
        params = deepcopy(self._default_templates["peeler"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        params["duration"] = key_params.get("duration", key_params.get("time", 30))
        params["peelingSpeed"] = key_params.get("peelingSpeed", key_params.get("speed", "medium"))
        params["comment"] = key_params.get("comment", "")
        
        return params
    
    def _build_incubator_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build incubator params"""
        params = deepcopy(self._default_templates["incubator"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        params["temperature_C"] = key_params.get("temperature_C", key_params.get("temperature", key_params.get("temp", 37)))
        params["rotationSpeed_rpm"] = key_params.get("rotationSpeed_rpm", key_params.get("speed", key_params.get("shake_speed", 200)))
        
        incubation_time = key_params.get("incubationTime", key_params.get("time", key_params.get("duration", 3600)))
        params["incubationTime"] = self._seconds_to_hms(incubation_time)
        
        return params
    
    def _build_centrifuge_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build centrifuge params"""
        params = deepcopy(self._default_templates["centrifuge"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        params["centrifugalForce_g"] = key_params.get("centrifugalForce_g", key_params.get("force", key_params.get("g", 3000)))
        params["temperature_C"] = key_params.get("temperature_C", key_params.get("temperature", 25))
        params["brake"] = key_params.get("brake", "medium")
        
        centrifugal_time = key_params.get("centrifugalTime", key_params.get("time", key_params.get("duration", 300)))
        params["centrifugalTime"] = self._seconds_to_hms(centrifugal_time)
        
        return params
    
    def _build_reader_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build plate reader params"""
        params = deepcopy(self._default_templates["plate_reader"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        params["plateType"] = key_params.get("plateType", key_params.get("plate_type", "96_clear"))
        params["detectionMode"] = key_params.get("detectionMode", key_params.get("mode", "absorbance"))
        params["volume_uL"] = key_params.get("volume_uL", key_params.get("volume", 200))
        params["detectionWavelength"] = key_params.get("detectionWavelength", key_params.get("wavelength", 450))
        params["enableShaking"] = key_params.get("enableShaking", key_params.get("shake", False))
        
        if key_params.get("excitation_nm"):
            params["excitation_nm"] = key_params["excitation_nm"]
        if key_params.get("emission_nm"):
            params["emission_nm"] = key_params["emission_nm"]
        
        return params
    
    def _build_washer_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build washer params"""
        params = deepcopy(self._default_templates["washer"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        params["cycleCount"] = key_params.get("cycleCount", key_params.get("cycles", 3))
        
        if "dispensingVolume_uL" in key_params or "volume" in key_params:
            params["dispensingConfig"]["dispensingVolume_uL"] = key_params.get("dispensingVolume_uL", key_params.get("volume", 300))
        
        return params
    
    def _build_refrigerator_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build refrigerator params"""
        params = deepcopy(self._default_templates["refrigerator"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        params["storageTemperature_C"] = key_params.get("storageTemperature_C", key_params.get("temperature", 4))
        
        storage_time = key_params.get("storageTime", key_params.get("time", key_params.get("duration", 3600)))
        params["storageTime"] = self._seconds_to_hms(storage_time)
        
        return params
    
    def _build_ce_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build capillary electrophoresis params"""
        params = deepcopy(self._default_templates["capillary_electrophoresis"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        params["sampleCount"] = key_params.get("sampleCount", key_params.get("samples", 96))
        params["sampleVolume_uL"] = key_params.get("sampleVolume_uL", key_params.get("volume", 5))
        params["sourceWellPosition"] = key_params.get("sourceWellPosition", key_params.get("well", "A1"))
        
        return params
    
    def _build_colony_picker_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build colony picker params"""
        params = deepcopy(self._default_templates["colony_picker"])
        
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("source_plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        target_consumable = key_params.get("targetConsumable", key_params.get("target_plate", ""))
        if target_consumable:
            params["targetConsumable"] = target_consumable
            self._get_or_create_consumable(target_consumable, node_id, self._match_consumable_id(target_consumable))
        
        params["colonyCount"] = key_params.get("colonyCount", key_params.get("colonies", 96))
        params["needle_depth_mm"] = key_params.get("needle_depth_mm", key_params.get("depth", 1.0))
        params["exposure_time_ms"] = key_params.get("exposure_time_ms", key_params.get("exposure", 100))
        
        return params
    
    def _build_generic_params(self, key_params: Dict, node_id: str) -> Dict:
        """Build generic parameters (fallback)"""
        params = {}
        
        # Handle consumables
        consumable_name = key_params.get("consumableName", key_params.get("consumable", key_params.get("plate", "")))
        if consumable_name:
            params["consumableName"] = consumable_name
            self._get_or_create_consumable(consumable_name, node_id, self._match_consumable_id(consumable_name))
        
        # Copy other parameters
        for key, value in key_params.items():
            if key not in params:
                params[key] = value
        
        return params
    
    def _build_consumables_list(self) -> List[Dict]:
        """
        Build consumables list - complies with Schema requirements
        
        Schema requirements:
        {
            "resourceId": int | null,
            "consumableName": string
        }
        """
        consumables = []
        
        for name, instance in self.consumable_instances.items():
            consumables.append({
                "resourceId": instance.consumable_id,
                "consumableName": instance.consumable_name
            })
        
        return consumables
    
resource_registry = ResourceRegistry()
parameter_builder = ParameterBuilder(resource_registry)
