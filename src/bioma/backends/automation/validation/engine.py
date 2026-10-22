"""Machine-code structure, resource/parameter validation and bounded repair."""
import jsonschema
import json
import re
from typing import List, Dict, Any, Set, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum
from bioma.backends.automation import settings


class ProtocolValidator:
    """
    Technical Validator
    Responsible for checking the machine code JSON for:
    1. JSON format correctness
    2. Schema structure integrity
    3. Resource ID validity (instruments and consumables)
    4. Connection graph integrity (no islands, no cycles)
    """
    
    # List of valid instrument IDs
    VALID_INSTRUMENT_IDS = {
        48, 49, 50, 51, 52, 53, 54, 55,
        56, 57, 58, 59, 60, 61, 62, 63,
        64, 65, 66, 67, 68, 69
    }
    
    # List of valid consumable IDs
    VALID_CONSUMABLE_IDS = {
        10023, 10024, 10025, 10026, 10027, 10028, 10029, 10030,
        10031, 10032, 10033, 10034, 10035, 10036, 10037, 10038,
        10039, 10040, 10041, 10042, 10043, 10044, 10051, 10052,
        10053, 10054
    }
    
    # JSON Schema 
    SCHEMA = {
        "type": "object",
        "required": ["nodes", "consumables", "connections"], #name,description
        "properties": {
            # "templateName": {"type": "string"},
            # "description": {"type": "string"},
            "nodes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["templateNodeId", "resourceId", "parameters"],
                    "properties": {
                        "templateNodeId": {"type": "integer"},
                        "resourceId": {"type": "integer"},
                        "parameters": {
                            "oneOf": [{"type": "null"}, {"type": "object"}]
                        }
                    }
                }
            },
            "consumables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["consumableName"], #resourceId
                    "properties": {
                        # "resourceId": {
                        #     "oneOf": [{"type": "null"}, {"type": "integer"}]
                        # },
                        "consumableName": {"type": "string"}
                    }
                }
            },
            "connections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["sourceNodeId", "targetNodeId"],
                    "properties": {
                        "sourceNodeId": {"type": "integer"},
                        "targetNodeId": {"type": "integer"}
                    }
                }
            }
        }
    }
    
    def __init__(self):
        self.valid_instruments = settings.VALID_INSTRUMENT_IDS
        self.valid_consumables = settings.VALID_CONSUMABLE_IDS
        self.rule_engine = RuleEngine()
    
    def extract_json(self, content: str) -> Tuple[Optional[Dict], str]:
        """
        Extract JSON from content
        Returns:
            (data, error_message) - error_message is an empty string on success
        """
        try:
            # Attempt to extract <exp_flow> tags
            match = re.search(r'<exp_flow>(.*?)</exp_flow>', content, re.DOTALL)
            if match:
                json_str = match.group(1).strip()
            else:
                json_str = content.strip()
            
            # Remove markdown code blocks
            if json_str.startswith("```"):
                json_str = re.sub(r'^```json?\s*', '', json_str)
                json_str = re.sub(r'\s*```$', '', json_str)
            
            data = json.loads(json_str)
            return data, ""
        except json.JSONDecodeError as e:
            return None, f"JSONError: {str(e)}"
    
    def check_schema(self, data: Dict) -> List[str]:
        """Check Schema structure"""
        errors = []
        try:
            jsonschema.validate(instance=data, schema=self.SCHEMA)
        except jsonschema.ValidationError as err:
            path = " -> ".join(str(p) for p in err.absolute_path) if err.absolute_path else "root"
            errors.append(f"SchemaError: {err.message} (Location: {path})")
        return errors
    
    def check_resources(self, data: Dict) -> List[str]:
        """Check resource ID validity"""
        errors = []
        
        # Check resourceId in nodes
        for node in data.get("nodes", []):
            res_id = node.get("resourceId")
            node_id = node.get("templateNodeId", "?")
            
            # --1 indicates manual/external operation (valid)
            if res_id == -1:
                continue
            
            try:
                res_id_int = int(res_id)
                if res_id_int not in self.valid_instruments:
                    errors.append(f"ResourceError: Node {node_id} uses invalid instrument ID {res_id_int}")
            except (TypeError, ValueError):
                errors.append(f"ResourceError: Node {node_id} has invalid resourceId format ({res_id})")
        
        # Check resourceId in consumables
        for i, con in enumerate(data.get("consumables", [])):
            res_id = con.get("resourceId")
            consumable_name = con.get("consumableName", f"consumable-{i + 1}")

            if res_id is None:
                continue
            
            try:
                res_id_int = int(res_id)
                if res_id_int not in self.valid_consumables:
                    errors.append(
                        f"ResourceError: Consumable {consumable_name!r} uses invalid ID {res_id_int}"
                    )
            except (TypeError, ValueError):
                errors.append(
                    f"ResourceError: Consumable {consumable_name!r} has invalid resourceId format ({res_id})"
                )
        
        return errors
    
    def check_connections(self, data: Dict) -> List[str]:
        """Check connection graph integrity"""
        errors = []
        
        nodes = data.get("nodes", [])
        if len(nodes) <= 1:
            # Single node or no nodes, no need to check connections
            return errors
        
        node_ids: Set[int] = set()
        for node in nodes:
            try:
                nid = int(node.get("templateNodeId", ""))
                node_ids.add(nid)
            except (TypeError, ValueError):
                errors.append("ConnectionError: Invalid format for node templateNodeId")
                return errors
        
        connections = data.get("connections", [])
        if not connections:
            errors.append(f"ConnectionError: {len(nodes)} nodes exist but no connections")
            return errors
        
        connected_nodes: Set[int] = set()
        graph: Dict[int, List[int]] = {nid: [] for nid in node_ids}
        
        for idx, conn in enumerate(connections):
            try:
                source = int(conn.get("sourceNodeId", ""))
                target = int(conn.get("targetNodeId", ""))
            except (TypeError, ValueError):
                errors.append(f"ConnectionError: Invalid node ID format for connection {idx+1}")
                continue
            
            if source not in node_ids:
                errors.append(f"ConnectionError: Source node {source} of the connection does not exist")
            if target not in node_ids:
                errors.append(f"ConnectionError: Target node {target} of the connection does not exist")
            
            connected_nodes.add(source)
            connected_nodes.add(target)
            if source in graph:
                graph[source].append(target)
        
        # Check for island nodes
        unconnected = node_ids - connected_nodes
        if unconnected:
            errors.append(f"ConnectionError: Nodes {sorted(unconnected)} are not covered by connections")
        
        # Detect cycles
        visited: Set[int] = set()
        rec_stack: Set[int] = set()
        
        def has_cycle(node: int) -> bool:
            if node not in visited:
                visited.add(node)
                rec_stack.add(node)
                for neighbor in graph.get(node, []):
                    if neighbor not in visited and has_cycle(neighbor):
                        return True
                    elif neighbor in rec_stack:
                        return True
                rec_stack.remove(node)
            return False
        
        for node in node_ids:
            if has_cycle(node):
                errors.append("ConnectionError: A cyclic path exists in the connections")
                break
        
        return errors
    
    def validate(self, content: str, verbose: bool = True, check_rules: bool = True) -> Tuple[bool, str]:
        """
        Perform full validation
        
        Args:
            content: String containing JSON
            verbose: Whether to print detailed information
            check_rules: Whether to execute rule checks
            
        Returns:
            (is_valid, message, rule_violations)
        """
        if verbose:
            print("--- Running Validator ---\n")
        
        data, error = self.extract_json(content)
        if data is None:
            return False, error, []
        
        if verbose:
            print("JSON format is valid\n")
        
        all_errors = []
        
        schema_errors = self.check_schema(data)
        if schema_errors:
            all_errors.extend(schema_errors)
        elif verbose:
            print("Schema validation passed\n")
        
        resource_errors = self.check_resources(data)
        if resource_errors:
            all_errors.extend(resource_errors)
        elif verbose:
            print("Resource ID validation passed\n")
        
        nodes = data.get("nodes", [])
        if len(nodes) > 1:
            conn_errors = self.check_connections(data)
            if conn_errors:
                all_errors.extend(conn_errors)
            elif verbose:
                print("Connection graph validation passed\n")
        elif verbose:
            print("Single-node workflow, no connection validation needed\n")
        
        if all_errors:
            return False, all_errors[0], []
        
        # 3. Rule checks (only after structural validation passes)
        rule_violations = []
        if check_rules:
            if verbose:
                print("--- Running Rule Engine ---\n")
            
            rule_violations = self.rule_engine.check(data)
            
            halt_violations = self.rule_engine.get_halt_violations()
            warn_violations = self.rule_engine.get_warn_violations()
            
            if verbose:
                if halt_violations:
                    print(f" Found {len(halt_violations)} halt errors\n")
                if warn_violations:
                    print(f"[Warning] Found {len(warn_violations)} warnings\n")
                if not rule_violations:
                    print("Rule checks passed\n")
            
            # HALT-level violations are treated as validation failures
            if halt_violations:
                return False, f"RuleError: {halt_violations[0].message}", rule_violations
        
        return True, "pass", rule_violations

# ============================================================================
#                           Part 2: Rule Engine
# ============================================================================

class Severity(Enum):
    """Severity level of rule violations"""
    HALT = "halt"  # Force stop (critical error)
    WARN = "warn"  # Warning (allow continuation)
    LOG = "log"    # Log only


@dataclass
class RuleViolation:
    """Rule violation record"""
    rule_id: str
    rule_name: str
    severity: Severity
    message: str
    node_id: Optional[int] = None
    details: Dict[str, Any] = field(default_factory=dict)


class RuleEngine():
    """
    Rule Engine - Checks the safety and scientific validity of experimental parameters
    
    Rule Categories:
    - LH: Liquid Handling
    - TC: Thermal Control
    - CF: Centrifugation
    - PS: Plate Sealing
    - SYS: System Rules
    """
    
    def __init__(
        self,
        instruments_path: str = str(settings.INSTRUMENTS_PATH),
    ):
        self.violations: List[RuleViolation] = []
        self.device_limits = self._load_device_limits(instruments_path)

    def _base_device_limits(self) -> Dict[int, Dict[str, Any]]:
        return {
            48: {"name": "Biomek i5", "min_volume_uL": 0.1, "max_volume_uL": 5000.0, "temp_range": (4, 100)},
            51: {"name": "Automatic Dispenser", "min_volume_uL": 1.0, "max_volume_uL": 3000.0},
            54: {"name": "Plate Washer", "min_volume_uL": 50.0, "max_volume_uL": 3000.0, "max_cycles": 10, "standingTime_range": (1, 10)},
            55: {"name": "SpectraMax i3x", "wavelength_range": (200, 1000), "min_volume_uL": 10, "max_volume_uL": 500},
            56: {"name": "LightCycler 480", "temp_range": (4, 110), "lid_temp_range": (95, 110)},
            57: {"name": "ATC PCR Machine", "min_volume_uL": 5.0, "max_volume_uL": 100.0, "temp_range": (4, 110), "lid_temp_range": (95, 110)},
            58: {"name": "Echo 525", "min_volume_uL": 0.5, "max_volume_uL": 5000.0},
            59: {"name": "Biomek i7", "min_volume_uL": 0.1, "max_volume_uL": 5000.0, "temp_range": (4, 100)},
            60: {"name": "LabChip GX Touch HT", "min_volume_uL": 10, "max_volume_uL": 40},
            61: {"name": "Automated Freezer", "temp_range": (-20, 10)},
            65: {"name": "Cytomat Incubator", "temp_range": (1, 120), "max_shake_rpm": 1000},
            66: {"name": "Rotanta 460", "max_g_force": 15000, "temp_range": (-10, 40)},
            67: {"name": "A4S Sealer", "temp_range": (10, 200)},
            69: {"name": "ATC PCR Machine-2", "min_volume_uL": 5.0, "max_volume_uL": 100.0, "temp_range": (4, 110), "lid_temp_range": (95, 110)},
        }

    def _get_full22_limits(self) -> Dict[int, Dict[str, Any]]:
        limits = self._base_device_limits()
        limits.update(
            {
                49: {"name": "Consumables Supply Station-1"},
                50: {"name": "Automated Plate Peeler", "peeling_speed_values": {"slow", "medium", "fast"}},
                52: {
                    "name": "Positive Pressure Module",
                    "pressure_bar_range": (0.5, 5.0),
                    "pressure_time_min_range": (1, 30),
                },
                53: {"name": "Composite Mobile Robot"},
                62: {
                    "name": "Automated Auxiliary Kit System",
                    "temp_range": (4, 95),
                    "shake_speed_rpm_range": (50, 2000),
                },
                63: {
                    "name": "Microbial Colony Picker",
                    "colony_count_range": (1, 384),
                    "needle_depth_mm_range": (0.1, 5.0),
                    "exposure_time_ms_range": (1, 2000),
                },
                64: {"name": "Consumables Supply Station-2"},
                68: {
                    "name": "Automated Nitrogen Evaporator",
                    "temp_range": (20, 80),
                    "gas_pressure_psi_range": (5, 30),
                    "plate_type_values": {"24", "96", "384"},
                    "needle_type_values": {"straight", "spiral"},
                },
            }
        )
        return limits

    def _load_device_limits(self, path: str) -> Dict[int, Dict]:
        """Load device limits from CSV and specifications field"""
        import csv
        from pathlib import Path

        limits = {}

        default_limits = self._get_full22_limits()
        csv_path = Path(path)
        if not csv_path.exists():
            return default_limits
        
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rid = int(row.get('resourceId', 0))

                    if rid not in settings.VALID_INSTRUMENT_IDS:
                        continue

                    specs = row.get('specifications', '')
                    device_name = row.get('deviceName', '')
                    
                    device_limit = default_limits.get(rid, {"name": device_name})
                    
                    parsed = self._parse_specifications(specs)
                    device_limit.update(parsed)
                    
                    limits[rid] = device_limit
                    
                except Exception:
                    pass
        
        for rid, default in default_limits.items():
            if rid not in limits:
                limits[rid] = default
        
        return limits
    
    def _parse_specifications(self, specs: str) -> Dict:
        """Parse numerical limits from specification descriptions"""
        result = {}
        
        if not specs:
            return result
        
        # Temp
        temp_patterns = [
            r'Temp.*?(\d+).*?[~-].*?(\d+).*?[鈩兟癈]',
            r'(\d+)[~-](\d+)\s*[鈩兟癈]',
        ]
        for pattern in temp_patterns:
            match = re.search(pattern, specs)
            if match:
                try:
                    low, high = int(match.group(1)), int(match.group(2))
                    result["temp_range"] = (min(low, high), max(low, high))
                    break
                except (TypeError, ValueError):
                    pass
        
        # Vol
        vol_patterns = [
            r'Vol.*?(\d+\.?\d*)[~-](\d+\.?\d*)\s*[u渭]?[lL]',
            r'(\d+\.?\d*)[~-](\d+\.?\d*)\s*[u渭][lL]',
        ]
        for pattern in vol_patterns:
            match = re.search(pattern, specs)
            if match:
                try:
                    low, high = float(match.group(1)), float(match.group(2))
                    result["min_volume_uL"] = min(low, high)
                    result["max_volume_uL"] = max(low, high)
                    break
                except (TypeError, ValueError):
                    pass
        
        g_pattern = r"(?:centrifugal force|g-force|rcf).*?(\d+)\s*[~-]\s*(\d+)\s*[x×]?[gG]"
        match = re.search(g_pattern, specs)
        if match:
            try:
                result["max_g_force"] = int(match.group(2))
            except (TypeError, ValueError):
                pass
        
        return result
    
    def check(self, data: Dict) -> List[RuleViolation]:
        """Execute rule checks"""
        self.violations = []
        
        nodes = data.get("nodes", [])
        connections = data.get("connections", [])
        
        self._build_node_order(nodes, connections)
        sorted_node = self._sort_node(nodes, connections)
        
        for node in nodes:
            resource_id = node.get("resourceId")
            node_id = node.get("templateNodeId")
            params = node.get("parameters", {}) or {}
            
            if resource_id == -1:
                self._check_external_node(node_id, params)
                continue
            
            device_limits = self.device_limits.get(resource_id, {})
            
            if not device_limits:
                self._add_violation(
                    "SYS-DEVICE-UNKNOWN", "Unknown Device", Severity.WARN,
                    f"No limit information configured for device {resource_id}",
                    node_id
                )
                continue

            self._check_temperature(node_id, params, device_limits,resource_id)
            self._check_volume(node_id, params, device_limits,resource_id)
            self._check_centrifuge(node_id, params, device_limits)
            if resource_id in {56, 57, 69}:
                self._check_pcr_program(node_id, params, device_limits)
            self._check_device_specific(node_id, resource_id, params, device_limits)
        
        self._check_seal_sequence(sorted_node)
        
        return self.violations
    
    def _build_node_order(self, nodes: List, connections: List) -> Dict[int, int]:
        """
        Build node execution order based on the connection graph
        
        Returns:
            Mapping of {node_id: order_index}
        """
        if not nodes:
            return {}
        
        node_ids = [n.get("templateNodeId") for n in nodes]
        
        if not connections:
            return {nid: idx for idx, nid in enumerate(sorted(node_ids))}

        graph = {nid: [] for nid in node_ids}
        in_degree = {nid: 0 for nid in node_ids}
        
        for conn in connections:
            src = conn.get("sourceNodeId")
            tgt = conn.get("targetNodeId")
            if src in graph and tgt in graph:
                graph[src].append(tgt)
                in_degree[tgt] += 1
        
        # Kahn's algorithm
        queue = [nid for nid in node_ids if in_degree[nid] == 0]
        order = {}
        idx = 0
        
        while queue:
            node = queue.pop(0)
            order[node] = idx
            idx += 1
            
            for neighbor in graph.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        
        for nid in node_ids:
            if nid not in order:
                order[nid] = idx
                idx += 1
        
        return order
    
    def _sort_node(self, nodes: List, connections: List) -> List:
        """
        Build node execution order based on the connection graph, return the sorted nodes list
        """
        if not nodes:
            return []
        
        # 1. Build mapping from node_id to node
        node_id_to_node = {n.get("templateNodeId"): n for n in nodes}
        node_ids = list(node_id_to_node.keys())
        
        # 2. Generate node_id -> order_index mapping (original logic unchanged)
        if not connections:
            sorted_node_ids = sorted(node_ids)
        else:
            graph = {nid: [] for nid in node_ids}
            in_degree = {nid: 0 for nid in node_ids}
            for conn in connections:
                src = conn.get("sourceNodeId")
                tgt = conn.get("targetNodeId")
                if src in graph and tgt in graph:
                    graph[src].append(tgt)
                    in_degree[tgt] += 1
            
            queue = [nid for nid in node_ids if in_degree[nid] == 0]
            sorted_node_ids = []
            while queue:
                node = queue.pop(0)
                sorted_node_ids.append(node)
                for neighbor in graph.get(node, []):
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
            # Handle unsorted nodes (cyclic nodes)
            sorted_node_ids.extend([nid for nid in node_ids if nid not in sorted_node_ids])
        
        # 3. Map back to original node objects using sorted node_ids
        sorted_nodes = [node_id_to_node[nid] for nid in sorted_node_ids]
        return sorted_nodes
    
    def _check_external_node(self, node_id: int, params: Dict):
        """Check external/manual nodes"""
        if not params.get("description") and not params.get("notes"):
            self._add_violation(
                "EXT-DESC-MISSING", "Missing External Operation Description", Severity.WARN,
                "External/manual operation nodes should include an operation description",
                node_id
            )
    
    def _check_temperature(self, node_id: int, params: Dict, limits: Dict, resource_id: int):
        """Universal temperature check"""
        temp_range = limits.get("temp_range")
        if not temp_range:
            return

        # Check various temperature fields
        temp_fields = ["temperature_C", "temperature", "temp", "storageTemperature_C", "targetTemperature_C", "targetTemperature"]
        
        if resource_id in {56, 57, 69}:
            return
        parameter_sets = params.get("workflowSteps", []) if resource_id in {48, 59} else [params]
        for values in parameter_sets:
            for field_name in temp_fields:
                if field_name in values:
                    temp = values[field_name]
                    if isinstance(temp, (int, float)) and not temp_range[0] <= temp <= temp_range[1]:
                        self._add_violation(
                            "TEMP-OUT-OF-RANGE", "Temperature Out of Range", Severity.HALT,
                            f"Temperature {temp}掳C exceeds device range {temp_range[0]}-{temp_range[1]}掳C",
                            node_id, {"field": field_name, "value": temp, "range": temp_range}
                        )

    def _check_volume(self, node_id: int, params: Dict, limits: Dict, resource_id: int):
        """Universal volume check"""
        min_vol = limits.get("min_volume_uL")
        max_vol = limits.get("max_volume_uL")
        
        if min_vol is None and max_vol is None:
            return
        
        # Check various volume fields
        vol_fields = ["volume", "volume_uL", "dispensingVolume","dispensingVolume_uL","reactionVolume_uL","reactionVolume","transferVolume","transferVolume_nL","sampleVolume","sampleVolume_uL"]

        parameter_sets = params.get("workflowSteps", []) if resource_id in {48, 59} else [params]
        for values in parameter_sets:
            for field_name in vol_fields:
                if field_name in values:
                    vol = values[field_name]
                    if not isinstance(vol, (int, float)):
                        continue
                    if min_vol is not None and vol < min_vol:
                        self._add_violation(
                            "VOL-TOO-SMALL", "Volume Too Small", Severity.HALT,
                            f"Pipetting volume {vol}uL is less than the minimum {min_vol}uL",
                            node_id
                        )
                    if max_vol is not None and vol > max_vol:
                        self._add_violation(
                            "VOL-TOO-LARGE", "Volume Too Large", Severity.HALT,
                            f"Pipetting volume {vol}uL exceeds the maximum {max_vol}uL",
                            node_id
                        )

    # ==================== Centrifuge Rules ====================
    
    def _check_centrifuge(self, node_id: int, params: Dict, limits: Dict):
        """Centrifuge check"""
        max_g = limits.get("max_g_force")
        if not max_g:
            return

        force_fields = ["centrifugalForce_g", "centrifugalForce"]
        g_force = None

        for force_field in force_fields:
            if force_field in params:
                g_force = params[force_field]

        if g_force is not None and isinstance(g_force, (int, float)) and g_force > max_g:
            self._add_violation(
                "CF-GFORCE-MAX", "Centrifugal Force Exceeds Limit", Severity.HALT,
                f"Centrifugal force {g_force}g exceeds device maximum {max_g}g",
                node_id
            )

    def _check_range_field(
        self,
        node_id: int,
        params: Dict,
        field: str,
        val_range: Tuple[float, float],
        rule_id: str,
        rule_name: str,
    ):
        if field not in params:
            return
        val = params.get(field)
        if not isinstance(val, (int, float)):
            return
        low, high = val_range
        if val < low or val > high:
            self._add_violation(
                rule_id,
                rule_name,
                Severity.HALT,
                f"{field}={val} out of range [{low}, {high}]",
                node_id,
                {"field": field, "value": val, "range": [low, high]},
            )

    def _check_device_specific(self, node_id: int, resource_id: int, params: Dict, limits: Dict):
        """Validate the configured 22-device registry."""

        # 49/64 Consumables supply stations: require at least one consumable hint.
        if resource_id in {49, 64}:
            has_supply_key = any(
                k in params for k in ["consumableName", "consumables", "materialName", "resourceQueue", "slots"]
            )
            if not has_supply_key:
                self._add_violation(
                    "SUPPLY-CONFIG-MISSING",
                    "Supply Station Missing Configuration",
                    Severity.WARN,
                    "Supply station node should declare consumable/material configuration.",
                    node_id,
                )
            return

        # 50 Plate peeler: duration and speed validity.
        if resource_id == 50:
            speed = str(params.get("peelingSpeed", "")).strip().lower()
            allowed = limits.get("peeling_speed_values", {"slow", "medium", "fast"})
            if "duration" in params and isinstance(params.get("duration"), (int, float)) and params.get("duration") <= 0:
                self._add_violation(
                    "PEEL-DURATION-INVALID",
                    "Invalid Peeling Duration",
                    Severity.HALT,
                    "duration must be > 0 for plate peeler.",
                    node_id,
                )
            if speed and speed not in allowed:
                self._add_violation(
                    "PEEL-SPEED-INVALID",
                    "Invalid Peeling Speed",
                    Severity.HALT,
                    f"peelingSpeed '{speed}' not in {sorted(allowed)}.",
                    node_id,
                )
            if not params.get("consumableName"):
                self._add_violation(
                    "PEEL-CONSUMABLE-MISSING",
                    "Missing Peeler Consumable",
                    Severity.WARN,
                    "Plate peeler node should provide consumableName.",
                    node_id,
                )
            return

        # 52 Positive pressure module.
        if resource_id == 52:
            self._check_range_field(
                node_id,
                params,
                "pressure_bar",
                limits.get("pressure_bar_range", (0.5, 5.0)),
                "PRESSURE-BAR-OUT-OF-RANGE",
                "Pressure Out of Range",
            )
            self._check_range_field(
                node_id,
                params,
                "pressureTime",
                limits.get("pressure_time_min_range", (1, 30)),
                "PRESSURE-TIME-OUT-OF-RANGE",
                "Pressure Time Out of Range",
            )
            return

        # 53 Composite mobile robot: require routing or description.
        if resource_id == 53:
            has_route = all(k in params for k in ["source", "target"]) or all(
                k in params for k in ["sourceNodeId", "targetNodeId"]
            )
            if not has_route and not (params.get("description") or params.get("task")):
                self._add_violation(
                    "MOBILE-ROUTE-MISSING",
                    "Missing Mobile Robot Route",
                    Severity.WARN,
                    "Composite mobile robot should provide route/source-target or task description.",
                    node_id,
                )
            return

        # 62 Auxiliary kit.
        if resource_id == 62:
            self._check_range_field(
                node_id,
                params,
                "shakeSpeed",
                limits.get("shake_speed_rpm_range", (50, 2000)),
                "AUX-SHAKE-OUT-OF-RANGE",
                "Shake Speed Out of Range",
            )
            return

        # 63 Colony picker.
        if resource_id == 63:
            self._check_range_field(
                node_id,
                params,
                "colonyCount",
                limits.get("colony_count_range", (1, 384)),
                "COLONY-COUNT-OUT-OF-RANGE",
                "Colony Count Out of Range",
            )
            self._check_range_field(
                node_id,
                params,
                "needle_depth_mm",
                limits.get("needle_depth_mm_range", (0.1, 5.0)),
                "COLONY-NEEDLE-DEPTH-OUT-OF-RANGE",
                "Needle Depth Out of Range",
            )
            self._check_range_field(
                node_id,
                params,
                "exposure_time_ms",
                limits.get("exposure_time_ms_range", (1, 2000)),
                "COLONY-EXPOSURE-OUT-OF-RANGE",
                "Exposure Time Out of Range",
            )
            if not params.get("targetConsumable"):
                self._add_violation(
                    "COLONY-TARGET-MISSING",
                    "Missing Colony Target Consumable",
                    Severity.WARN,
                    "Colony picker should provide targetConsumable.",
                    node_id,
                )
            return

        # 68 Nitrogen evaporator.
        if resource_id == 68:
            self._check_range_field(
                node_id,
                params,
                "gas_pressure",
                limits.get("gas_pressure_psi_range", (5, 30)),
                "N2-PRESSURE-OUT-OF-RANGE",
                "Nitrogen Pressure Out of Range",
            )
            if "duration" in params and isinstance(params.get("duration"), (int, float)) and params.get("duration") <= 0:
                self._add_violation(
                    "N2-DURATION-INVALID",
                    "Invalid Nitrogen Evaporation Duration",
                    Severity.HALT,
                    "duration must be > 0 for nitrogen evaporator.",
                    node_id,
                )
            plate_type = str(params.get("plateType", "")).strip()
            allowed_plate = limits.get("plate_type_values", {"24", "96", "384"})
            if plate_type and plate_type not in allowed_plate:
                self._add_violation(
                    "N2-PLATE-TYPE-INVALID",
                    "Invalid Plate Type",
                    Severity.HALT,
                    f"plateType '{plate_type}' not in {sorted(allowed_plate)}.",
                    node_id,
                )
            needle_type = str(params.get("needleType", "")).strip().lower()
            allowed_needle = limits.get("needle_type_values", {"straight", "spiral"})
            if needle_type and needle_type not in allowed_needle:
                self._add_violation(
                    "N2-NEEDLE-TYPE-INVALID",
                    "Invalid Needle Type",
                    Severity.HALT,
                    f"needleType '{needle_type}' not in {sorted(allowed_needle)}.",
                    node_id,
                )
            return
    
    def _check_pcr_program(self, node_id: int, params: Dict, limits: Dict):
        """PCR program check"""
        temp_range = limits.get("temp_range")
        lid_range = limits.get("lid_temp_range")

        if not temp_range and not lid_range:
            return

        # Lid temperature
        lid_temp = params.get("lidTemperature_C")
        if lid_temp and lid_range:
            if lid_temp < lid_range[0] or lid_temp > lid_range[1]:
                self._add_violation(
                    "PCR-LID-TEMP", "Lid Temperature Exceeds Limit", Severity.HALT,
                    f"Lid temperature {lid_temp}掳C is out of range {lid_range[0]}-{lid_range[1]}掳C",
                    node_id
                )

        # Program step temperature
        max_program_temp = 0
        steps = params.get("programSteps") or params.get("temperatureProgram") or []
        for step in steps:
            if "temperature_C" in step:
                temp = step["temperature_C"]
                max_program_temp = max(max_program_temp, temp)

                if temp_range and (temp < temp_range[0] or temp > temp_range[1]):
                    self._add_violation(
                        "PCR-STEP-TEMP", "Program Step Temperature Exceeds Limit", Severity.HALT,
                        f"Program temperature {temp}掳C is out of device range",
                        node_id
                    )

        # Lid temperature must be higher than the maximum program temperature
        if lid_temp and max_program_temp > 0 and lid_temp <= max_program_temp:
            self._add_violation(
                "PCR-LID-LOW", "Lid Temperature Too Low", Severity.HALT,
                f"Lid temperature {lid_temp}掳C must be higher than the maximum program temperature {max_program_temp}掳C",
                node_id
            )
    
    def _check_seal_sequence(self, nodes: List):
        """Check sealing/peeling sequence"""
        pcr_incubator_ids = {57, 69, 56, 65}
        sealer_id = 67

        for i, node in enumerate(nodes):
            rid = node.get("resourceId")
            
            if rid in pcr_incubator_ids:
                # Check if there's a sealer node before current node
                has_seal = any(
                    nodes[j].get("resourceId") == sealer_id
                    for j in range(i)
                )
                
                if not has_seal:
                    self._add_violation(
                        "SEQ-NO-SEAL", "Missing Sealing Step", Severity.WARN,
                        f"Node {node.get('templateNodeId')} (PCR/Incubation) should be preceded by a sealing operation",
                        node.get("templateNodeId")
                    )
    
    def _add_violation(
        self, 
        rule_id: str, 
        rule_name: str, 
        severity: Severity, 
        message: str,
        node_id: Optional[int] = None,
        details: Dict = None
    ):
        self.violations.append(RuleViolation(
            rule_id=rule_id,
            rule_name=rule_name,
            severity=severity,
            message=message,
            node_id=node_id,
            details=details or {}
        ))
    
    def get_halt_violations(self) -> List[RuleViolation]:
        return [v for v in self.violations if v.severity == Severity.HALT]
    
    def get_warn_violations(self) -> List[RuleViolation]:
        return [v for v in self.violations if v.severity == Severity.WARN]

# ============================================================================
#                           Part 4: Smart Fixer
# ============================================================================

class SmartFixer:
    """
    Smart Fixer
    
    Repair Strategies:
    1. Exact Match: Direct ID mapping
    2. Fuzzy Match: Based on name similarity
    3. Function Match: Based on device capabilities
    4. Degradation Handling: Set to -1 when no match is possible
    """

    VALID_INSTRUMENT_IDS = settings.VALID_INSTRUMENT_IDS
    VALID_CONSUMABLE_IDS = settings.VALID_CONSUMABLE_IDS

    
    # Device function classification
    DEVICE_FUNCTIONS = {
        "liquid_handling": [48, 59],  
        "pcr": [57, 69],             
        "qpcr": [56],                 
        "sealing": [67],              
        "peeling": [50],              
        "centrifuge": [66],          
        "incubation": [65],           
        "detection": [55],            
        "washing": [54],              
        "storage": [61],             
        "electrophoresis": [60],     
        "colony_picking": [63],       
    }
    
    # Action-to-Function Mapping
    ACTION_TO_FUNCTION = {
        "绉绘恫": "liquid_handling",
        "transfer": "liquid_handling",
        "鍔犳牱": "liquid_handling",
        "鍒嗚": "liquid_handling",
        "娣峰寑": "liquid_handling",
        "mix": "liquid_handling",
        "pipette": "liquid_handling",
        
        "pcr": "pcr",
        "鎵╁": "pcr",
        "amplification": "pcr",
        "thermal_cycle": "pcr",
        
        "qpcr": "qpcr",
        "鑽у厜瀹氶噺": "qpcr",
        "real-time": "qpcr",

        "灏佽啘": "sealing",
        "seal": "sealing",
        "鎾曡啘": "peeling",
        "peel": "peeling",
        "鍘昏啘": "peeling",
        
        "绂诲績": "centrifuge",
        "centrifuge": "centrifuge",
        
        "瀛佃偛": "incubation",
        "鍩瑰吇": "incubation",
        "incubate": "incubation",
        
        "detect": "detection",
        "璇绘暟": "detection",
        "read": "detection",
        "閰舵爣": "detection",
        
        "娲楁澘": "washing",
        "wash": "washing",
        "娓呮礂": "washing",
        
        "瀛樺偍": "storage",
        "鍐疯棌": "storage",
        "store": "storage",
        
        "鐢垫吵": "electrophoresis",
        
        "colony_pick": "colony_picking",
        "pick": "colony_picking",
    }
    
    # Consumable Keyword Mapping
    CONSUMABLE_KEYWORDS = {
        10036: ["pcr plate", "pcr", "200ul"],
        10037: ["deep well plate", "1.1ml", "deep well"],
        10038: ["deep well plate", "2.2ml"],
        10035: ["reservoir", "reservoir", "reagent trough"],
        10039: ["microplate", "white plate", "luminescence"],
        10040: ["microplate", "black plate", "fluorescence"],
        10041: ["microplate", "transparent", "absorbance"],
        10051: ["omnitray", "petri dish"],
        10052: ["agar plate"],
        10053: ["magnetic bead plate", "kingfisher"],
        10054: ["deep well plate", "round well"],
        10043: ["qpcr plate", "384-well", "fluorescent quantification"],
        10044: ["qpcr plate", "96-well", "fluorescent quantification"],
    }
    
    def __init__(self):
        self.fix_logs: List[str] = []
    
    def fix(self, data: Dict) -> Tuple[Dict, List[str]]:
        """
        Repair JSON structure
        
        Returns:
            (fixed_data, fix_logs)
        """
        self.fix_logs = []
        
        # 1. Ensure required fields
        data = self._ensure_required_fields(data)

        # 2. Fix nodes
        data = self._fix_nodes(data)

        # 3. Fix consumables
        data = self._fix_consumables(data)

        # 4. Fix connections
        data = self._fix_connections(data)
        
        return data, self.fix_logs
    
    def _ensure_required_fields(self, data: Dict) -> Dict:
        """Ensure required fields exist"""
        defaults = {
            "templateName": "Experiment Workflow",
            "description": "",
            "nodes": [],
            "connections": [],
            "consumables": [],
        }

        for key, default in defaults.items():
            if key not in data:
                data[key] = default
                self.fix_logs.append(f"Added missing field: {key}")

        return data
    
    def _fix_nodes(self, data: Dict) -> Dict:
        """Fix Nodes"""
        fixed_nodes = []
        
        for node in data.get("nodes", []):
            fixed_node = self._fix_single_node(node)
            fixed_nodes.append(fixed_node)
        
        data["nodes"] = fixed_nodes
        return data
    
    def _fix_single_node(self, node: Dict) -> Dict:
        """Repair a single node"""
        node_id = node.get("templateNodeId", "?")

        # Ensure parameters exist
        if "parameters" not in node or node["parameters"] is None:
            node["parameters"] = {}
            self.fix_logs.append(f"Node {node_id}: Added empty parameters")

        # Fix resourceId
        resource_id = node.get("resourceId")
        action = node.get("action", "")

        if resource_id is None:
            # Try to infer from action
            inferred_id = self._infer_device_from_action(action)
            node["resourceId"] = inferred_id
            self.fix_logs.append(f"Node {node_id}: resourceId null → {inferred_id} (inferred)")

        elif resource_id == -1:
            # Keep as manual/external node
            self._ensure_external_params(node)

        elif resource_id not in self.VALID_INSTRUMENT_IDS:
            # Invalid ID, attempt repair
            fixed_id = self._fix_invalid_instrument_id(resource_id, action, node)
            node["resourceId"] = fixed_id
            self.fix_logs.append(f"Node {node_id}: resourceId {resource_id} → {fixed_id}")

        return node
    
    def _infer_device_from_action(self, action: str) -> int:
        """Infer device ID from action"""
        action_lower = action.lower()

        for keyword, function in self.ACTION_TO_FUNCTION.items():
            if keyword in action_lower:
                devices = self.DEVICE_FUNCTIONS.get(function, [])
                if devices:
                    return devices[0]  # Return the preferred device for this function

        return -1  # Cannot infer, mark as manual
    
    def _fix_invalid_instrument_id(self, invalid_id: int, action: str, node: Dict) -> int:
        """Fix invalid instrument ID"""
        # Strategy 1: Check for common invalid ID patterns
        # e.g., 1001, 1002 (LLM-generated IDs)
        if 1000 <= invalid_id <= 9999:
            # Attempt action-based inference
            inferred = self._infer_device_from_action(action)
            if inferred != -1:
                return inferred

        # Strategy 2: Check if it's close to a valid ID
        closest = min(self.VALID_INSTRUMENT_IDS,
                    key=lambda x: abs(x - invalid_id))
        if abs(closest - invalid_id) <= 5:
            return closest

        # Strategy 3: Infer from node name
        node_name = node.get("nodeName", "")
        inferred = self._infer_device_from_name(node_name)
        if inferred != -1:
            return inferred

        # Strategy 4: Cannot fix, mark as manual
        return -1
    
    def _infer_device_from_name(self, name: str) -> int:
        """Infer device ID from node name"""
        name_lower = name.lower()

        # Name-to-ID mapping
        name_mappings = {
            "biomek": 59,
            "i7": 59,
            "i5": 48,
            "pcr machine": 57,
            "atc": 57,
            "lightcycler": 56,
            "qpcr": 56,
            "sealing": 67,
            "a4s": 67,
            "peel": 50,
            "xp-a": 50,
            "centrifuge": 66,
            "rotanta": 66,
            "incubation": 65,
            "cytomat 2c450": 65,
            "microplate reader": 55,
            "spectramax": 55,
            "plate washing": 54,
            "freezer": 61,
            "electrophoresis": 60,
            "labchip": 60,
            "cloning": 63,
            "k8": 63,
        }

        for keyword, rid in name_mappings.items():
            if keyword in name_lower:
                return rid

        return -1
    
    def _ensure_external_params(self, node: Dict):
        """Ensure external/manual nodes have necessary parameters"""
        params = node.get("parameters", {})

        if "operationType" not in params:
            params["operationType"] = "manual"

        if "deviceName" not in params:
            action = node.get("action", "")
            params["deviceName"] = action if action else "Manual Operation"

        if "description" not in params and "notes" not in params:
            params["description"] = node.get("action", "Manual Operation Step")

        node["parameters"] = params
    
    def _fix_consumables(self, data: Dict) -> Dict:
        """Repair consumables list"""
        fixed_consumables = []
        seen_names = set()

        for con in data.get("consumables", []):
            name = con.get("consumableName", con.get("name", ""))
            if not name or name in seen_names:
                continue

            seen_names.add(name)

            # Fix resourceId
            res_id = con.get("resourceId", con.get("consumableId"))

            if res_id is not None:
                try:
                    res_id = int(res_id)
                    if res_id not in self.VALID_CONSUMABLE_IDS:
                        # Attempt matching
                        matched = self._match_consumable(name)
                        if matched:
                            self.fix_logs.append(f"Consumable '{name}': {res_id} → {matched}")
                            res_id = matched
                        else:
                            self.fix_logs.append(f"Consumable '{name}': {res_id} → null (invalid)")
                            res_id = None
                except (TypeError, ValueError):
                    res_id = self._match_consumable(name)
            else:
                res_id = self._match_consumable(name)

            fixed_consumables.append({
                "resourceId": res_id,
                "consumableName": name
            })

        data["consumables"] = fixed_consumables
        return data
    
    def _match_consumable(self, name: str) -> Optional[int]:
        """Match Consumable ID"""
        name_lower = name.lower()
        
        best_match = None
        best_score = 0
        
        for cid, keywords in self.CONSUMABLE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in name_lower)
            if score > best_score:
                best_score = score
                best_match = cid
        
        if best_match:
            return best_match
        
        
        if "plate" in name_lower:
            return 10036
        
        return None
    
    def _fix_connections(self, data: Dict) -> Dict:
        """Repair connections"""
        nodes = data.get("nodes", [])
        connections = data.get("connections", [])

        if len(nodes) <= 1:
            data["connections"] = []
            return data

        # Get all node IDs
        node_ids = [n.get("templateNodeId") for n in nodes]
        valid_node_ids = set(node_ids)

        # Filter valid connections
        valid_connections = []
        for conn in connections:
            src = conn.get("sourceNodeId")
            tgt = conn.get("targetNodeId")
            if src in valid_node_ids and tgt in valid_node_ids:
                valid_connections.append(conn)

        # Check if all nodes are covered
        connected = set()
        for conn in valid_connections:
            connected.add(conn.get("sourceNodeId"))
            connected.add(conn.get("targetNodeId"))
        
        unconnected = valid_node_ids - connected
        
        if unconnected and len(valid_connections) > 0:
            # Attempt repair: Link unconnected nodes to the end
            last_connected = valid_connections[-1].get("targetNodeId")
            for uc_id in sorted(unconnected):
                valid_connections.append({
                    "sourceNodeId": last_connected,
                    "targetNodeId": uc_id
                })
                self.fix_logs.append(f"Added connection: {last_connected} → {uc_id}")
                last_connected = uc_id

        elif not valid_connections and len(nodes) > 1:
            # No connections, create linear connections
            for i in range(len(node_ids) - 1):
                valid_connections.append({
                    "sourceNodeId": node_ids[i],
                    "targetNodeId": node_ids[i + 1]
                })
            self.fix_logs.append(f"Created linear connections: {len(valid_connections)} entries")

        data["connections"] = valid_connections
        return data

    def _fix_params(self, data: Dict) -> Dict:
        """Fix the detail parameters of each node, such as temperature, volume, centrifuge force, etc."""
        nodes = data.get("nodes", [])

        default_limits = {
            48: {"name": "Biomek i5", "min_volume_uL": 0.1, "max_volume_uL": 5000.0, "temp_range": (4, 100)},
            51: {"name": "Auto Dispenser", "min_volume_uL": 1.0, "max_volume_uL": 3000.0},
            54: {
                "name": "Plate Washer",
                "min_volume_uL": 50.0,
                "max_volume_uL": 3000.0,
                "max_cycles": 10,
                "standingTime_range": (1, 10),
            },
            55: {"name": "SpectraMax i3x", "wavelength_range": (200, 1000), "min_volume_uL": 10, "max_volume_uL": 500},
            56: {"name": "LightCycler 480", "temp_range": (4, 110), "lid_temp_range": (95, 110)},
            57: {
                "name": "ATC PCR",
                "min_volume_uL": 5.0,
                "max_volume_uL": 100.0,
                "temp_range": (4, 110),
                "lid_temp_range": (95, 110),
            },
            58: {"name": "Echo 525", "min_volume_uL": 0.5, "max_volume_uL": 5000.0},
            59: {"name": "Biomek i7", "min_volume_uL": 0.1, "max_volume_uL": 5000.0, "temp_range": (4, 100)},
            60: {"name": "LabChip GX Touch HT", "min_volume_uL": 10, "max_volume_uL": 40},
            61: {"name": "Auto Refrigerator", "temp_range": (-20, 10)},
            65: {"name": "Cytomat Incubator", "temp_range": (1, 120), "max_shake_rpm": 1000},
            66: {"name": "Rotanta 460", "max_g_force": 15000, "temp_range": (-10, 40)},
            67: {"name": "A4S Sealer", "temp_range": (10, 200)},
            69: {
                "name": "ATC PCR 2",
                "min_volume_uL": 5.0,
                "max_volume_uL": 100.0,
                "temp_range": (4, 110),
                "lid_temp_range": (95, 110),
            },
        }

        for node in nodes:
            resource_id = node.get("resourceId")
            node_id = node.get("templateNodeId")
            params = node.get("parameters", {}) or {}

            
            device_limits = default_limits.get(resource_id, {})
            
            if not device_limits:
                continue
            
            data = self._fix_temperature(data, node_id, params, device_limits,resource_id)
            data = self._fix_volume(data, node_id, params, device_limits,resource_id)
            data = self._fix_centrifuge(data, node_id, params, device_limits)
        
        return data

    # Fix centrifuge parameters
    def _fix_centrifuge(self, data: Dict[str, Any], node_id: int, params: Dict[str, Any], limits: Dict[str, Any]) -> Dict[str, Any]:
        """
        Check and fix centrifuge force parameters
        """
        max_g = limits.get("max_g_force")
        if not max_g:
            return data  
        
        force_fields = ["centrifugalForce_g","centrifugalForce"]
        g_force = None
        target_field = None

        for force_field in force_fields:
            if force_field in params:
                g_force = params[force_field]
                target_field = force_field
                break

        if g_force is not None and isinstance(g_force, (int, float)):
            if g_force > max_g:
                for node in data.get("nodes", []):
                    current_node_id = node.get("templateNodeId")
                    if current_node_id == node_id:
                        node["parameters"][target_field] = max_g/2
                        self.fix_logs.append(f"Fixed the over centrifugal force node: {node_id}. Set {target_field} from {g_force}g to {max_g/2}g.")
                        print(f"Fixed the over centrifugal force node: {node_id}. Set {target_field} from {g_force}g to {max_g/2}g.")
                        break
        return data

    # Fix temperature parameters
    def _fix_temperature(self, data: Dict[str, Any], node_id: int, params: Dict, limits: Dict, resource_id: int):
        """General temperature check"""
        temp_range = limits.get("temp_range")
        if not temp_range:
            return data
        
        default_temp = (temp_range[0] + temp_range[1])/2

        temp_fields = ["temperature_C", "temperature", "temp", "storageTemperature_C", "targetTemperature_C", "targetTemperature"]
        
        if resource_id in (48, 59):
            for step_idx, step in enumerate(params.get("workflowSteps", [])):
                for field in temp_fields:
                    if field in step:
                        temp = step[field]
                        if isinstance(temp, (int, float)):
                            if temp < temp_range[0] or temp > temp_range[1]:
                                for node in data.get("nodes", []):
                                    if node.get("templateNodeId") == node_id:
                                        workflow_steps = node["parameters"].get("workflowSteps", [])
                                        if step_idx < len(workflow_steps):
                                            workflow_steps[step_idx][field] = default_temp
                                            self.fix_logs.append(f"Fixed the over temperature node: {node_id}. Set {field} from {temp} to {default_temp}.")
                                            print(f"Fixed the over temperature node: {node_id}. Set {field} from {temp} to {default_temp}.")
                                        break
            return data
        # Check for pcr instruments to check each step
        if resource_id in (56, 57, 69):
            for step_idx, step in enumerate(params.get("programSteps", [])):
                for field in temp_fields:
                    if field in step:  # Check temperature fields within each step (core fix)
                        temp = step[field]
                        if isinstance(temp, (int, float)):
                            if temp < temp_range[0] or temp > temp_range[1]:
                                for node in data.get("nodes", []):
                                    if node.get("templateNodeId") == node_id:
                                        program_steps = node["parameters"].get("programSteps", [])
                                        if step_idx < len(program_steps):
                                            program_steps[step_idx][field] = default_temp
                                            self.fix_logs.append(f"Fixed the over temperature node: {node_id}. Set {field} from {temp} to {default_temp}.")
                                            print(f"Fixed the over temperature node: {node_id}. Set {field} from {temp} to {default_temp}.")
                                        break
            return data

        # For other devices, check individual temperature settings
        for field in temp_fields:
            if field in params:
                temp = params[field]
                if isinstance(temp, (int, float)):
                    if temp < temp_range[0] or temp > temp_range[1]:
                        for node in data.get("nodes", []):
                            current_node_id = node.get("templateNodeId")
                            if current_node_id == node_id:
                                node["parameters"][field] = default_temp
                                self.fix_logs.append(f"Fixed the over temperature node: {node_id}. Set {field} from {temp} to {default_temp}.")
                                print(f"Fixed the over temperature node: {node_id}. Set {field} from {temp} to {default_temp}.")
                                break
        return data


    # Fix volume parameters
    def _fix_volume(self, data ,node_id: int, params: Dict, limits: Dict, resource_id: int):
        """General volume check and correction."""
        min_vol = limits.get("min_volume_uL")
        max_vol = limits.get("max_volume_uL")
        
        if min_vol is None and max_vol is None:
            return data
        
        # Check various volume fields
        vol_fields = ["volume", "volume_uL", "dispensingVolume","dispensingVolume_uL","reactionVolume_uL","reactionVolume","transferVolume","transferVolume_nL","sampleVolume","sampleVolume_uL"]

        # For pipettes, check each step
        if resource_id in (48, 59):
            for step_idx, step in enumerate(params.get("workflowSteps", [])):
                for field in vol_fields:
                    if field in step:  
                        vol = step[field]
                        if isinstance(vol, (int, float)):
                            fixed_vol = vol
                            violation_msg = ""
                            # Fix too small value
                            if min_vol is not None and vol < min_vol:
                                fixed_vol = min_vol
                                violation_msg = f"Step {step_idx+1} has {field}={vol}碌L which is less than the minimum value {min_vol}碌L. Fixed to {min_vol}碌L."
                            # Fix too large value
                            elif max_vol is not None and vol > max_vol:
                                fixed_vol = max_vol
                                violation_msg = f"Step {step_idx+1} has {field}={vol}碌L which exceeds the maximum value {max_vol}碌L. Fixed to {max_vol}碌L"
                            
                            # Report and check if there is a violation
                            if violation_msg:
                                # Fix the volume value in the data
                                for node in data.get("nodes", []):
                                    if node.get("templateNodeId") == node_id:
                                        # Ensure workflowSteps exists and the index is valid
                                        workflow_steps = node["parameters"].get("workflowSteps", [])
                                        if step_idx < len(workflow_steps):
                                            workflow_steps[step_idx][field] = fixed_vol
                                            self.fix_logs.append(f"Fixed the over limit volume in node {node_id}.")
                                            print(f"Fixed the over limit volume in node {node_id}.")
                                        break
            return data
        
        # For other devices, check individual volume settings
        for field in vol_fields:
            if field in params:
                vol = params[field]
                if isinstance(vol, (int, float)):
                    fixed_vol = vol
                    violation_msg = ""
                    # Check and fix too small value
                    if min_vol is not None and vol < min_vol:
                        fixed_vol = min_vol
                        violation_msg = f"{field}={vol}碌L is less than the minimum value {min_vol}碌L. Fixed to {min_vol}碌L."
                    # Check and fix too large value
                    elif max_vol is not None and vol > max_vol:
                        fixed_vol = max_vol
                        violation_msg = f"{field}={vol}碌L exceeds the maximum value {max_vol}碌L. Fixed to {max_vol}碌L"
                    
                    # If there is a violation, record and modify the data
                    if violation_msg:
                        # Modify the top-level parameters of the corresponding node in the data
                        for node in data.get("nodes", []):
                            if node.get("templateNodeId") == node_id:
                                node["parameters"][field] = fixed_vol
                                self.fix_logs.append(f"Fixed the over limit volume in node {node_id}.")
                                print(f"Fixed the over limit volume in node {node_id}.")
                                break
        return data

# 鍏ㄥ眬瀹炰緥
smart_fixer = SmartFixer()

# ============================================================================
#                           Part 5: Utility Functions
# ============================================================================

_validator = ProtocolValidator()
_fixer = SmartFixer()


def validate_machine_code(content: str, verbose: bool = True, check_rules: bool = True):
    # Validate machine code (utility function).
    return _validator.validate(content, verbose=verbose, check_rules=check_rules)


def fix_machine_code_core(content: str) -> Tuple[bool, str, str]:
    # Fix machine code and return (is_valid, message, fixed_json_str).
    print("--- Smart Fixer ---\n")

    # 1. Extract JSON
    data = None
    try:
        match = re.search(r'<exp_flow>(.*?)</exp_flow>', content, re.DOTALL)
        if match:
            data = json.loads(match.group(1).strip())
        else:
            data = json.loads(content.strip())
    except json.JSONDecodeError as e:
        return False, f"JSON parsing failed: {e}", content

    # 2. Execute repair
    fixed_data, logs = smart_fixer.fix(data)

    if logs:
        print(f"Fixed {len(logs)} issues:")
        for log in logs[:10]:
            print(f" - {log}")
        if len(logs) > 10:
            print(f" ... and {len(logs) - 10} more")
    
    # 3. Build result
    fixed_json = f"<exp_flow>\n{json.dumps(fixed_data, indent=2, ensure_ascii=False)}\n</exp_flow>"

    # 4. Simple validation
    nodes = fixed_data.get("nodes", [])
    connections = fixed_data.get("connections", [])

    # Check basic structure
    if not nodes:
        return False, "No nodes after repair", fixed_json

    # Check resourceId
    invalid_ids = []
    for node in nodes:
        rid = node.get("resourceId")
        if rid != -1 and rid not in SmartFixer.VALID_INSTRUMENT_IDS:
            invalid_ids.append(rid)

    if invalid_ids:
        return False, f"Still has invalid resourceId: {invalid_ids}", fixed_json

    # Check connections
    if len(nodes) > 1 and not connections:
        return False, "Multiple nodes but no connections", fixed_json

    message = "Repair successful" if logs else "No repair needed"
    if logs:
        message = f"Fixed {len(logs)} issues: " + "; ".join(logs[:3])

    return True, message, fixed_json
