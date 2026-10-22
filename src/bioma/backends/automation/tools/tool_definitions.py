"""The four automation tools used by ConfiguredAutomationCompiler."""
import json
import re
from typing import Optional, Dict
from langchain_core.tools import tool

from bioma.backends.automation.provider import invoke
from bioma.backends.automation.validation.engine import (
    validate_machine_code as validator_check,
    fix_machine_code_core as fixer_fix,
    ProtocolValidator,
)
from bioma.backends.automation.parameters.parameter_processing import parameter_builder
from bioma.backends.automation.prompts.prompts import build_alignment_prompt, build_paint_prompt


def _extract_simplified_flow(content: str) -> Optional[Dict]:
    """Parse experiment flow JSON"""
    try:
        # 1. First attempt to extract content within <exp_flow> tags
        match = re.search(r'<exp_flow>(.*?)</exp_flow>', content, re.DOTALL)
        if match:
            json_str = match.group(1).strip()
        else:
            json_str = content.strip()
        
        # 2. Remove markdown code blocks
        if json_str.startswith("```"):
            json_str = re.sub(r'^```json?\s*', '', json_str)
            json_str = re.sub(r'\s*```$', '', json_str)
        
        # 3. Parse JSON
        return json.loads(json_str)
        
    except json.JSONDecodeError as e:
        print(f" JSON parsing failed: {e}")
        try:
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end != -1 and end > start:
                json_str = content[start:end+1]
                return json.loads(json_str)
        except json.JSONDecodeError:
            pass
        return None
    except Exception as e:
        print(f" parsing failed: {e}")
        return None


@tool
def align_draft_to_automation(draft: str, exp_info: str, doc_content: Optional[str] = None) -> str:
    """
    Call this tool to convert the scientific protocol into detailed step-by-step text aligned with laboratory automation equipment.

    This tool labels each step with an execution type:
    - [AUTO]: Executable on the current automation platform
    - [EXTERNAL]: Requires external equipment (available in the laboratory but not on the automation platform)
    - [MANUAL]: Requires manual operation

    parameters: draft: str, exp_info: str, doc_content: Optional[str] = None
    Output: A standardized, line-by-line descriptive string, serving as input for JSON canvas generation in the next step.
    """
    print("--- Tool: Protocol to Equipment Alignment ---")
    draft = str(draft) if draft else ""
    exp_info = str(exp_info) if exp_info else ""
    doc_content = str(doc_content) if doc_content else ""
    
    prompt = build_alignment_prompt(
        exp_info=exp_info,
        protocol=draft,
        document=doc_content
    )

    aligned_protocol = invoke(prompt).content
    
    print("--- Equipment Alignment Completed ---")
    print("  - Execution Type Label: [AUTO]/[EXTERNAL]/[MANUAL]")
    
    return aligned_protocol


@tool
def generate_machine_code(aligned_protocol: str, exp_info: str = "", suggestion: str = "[N/A]", session_id: str = "") -> str:
    """
    Call this tool to generate Machine Code via a Two-Stage Generation
    parameters: aligned_protocol: str, exp_info: str = "", suggestion: str = "[N/A]", session_id: str = ""
    """
    print("--- Tool: Generate Machine Code ---")
    prompt = build_paint_prompt(
        protocol=aligned_protocol,
        exp_info=exp_info,
        suggestion=suggestion
    )

    response = invoke(prompt)
    raw_output = response.content.strip()

    simplified_flow = _extract_simplified_flow(raw_output)

    if not simplified_flow:
        print(" LLM output parsing failed, attempting to use raw output directly")
        if "<exp_flow>" in raw_output:
            return raw_output
        return f"<exp_flow>\n{raw_output}\n</exp_flow>"

    print("--- Stage 2: Parameter Population ---")
    
    try:
        full_flow = parameter_builder.build_experiment_flow(simplified_flow)
        
        auto_count = sum(1 for n in full_flow.get("nodes", []) if n.get("resourceId", -1) != -1)
        manual_count = len(full_flow.get("nodes", [])) - auto_count
        
        print(f"  - Automated Nodes: {auto_count}")
        print(f"  - Manual/External Nodes: {manual_count}")
        
    except Exception as e:
        print(f" Parameter population failed: {e}")
        full_flow = simplified_flow

    result = f"<exp_flow>\n{json.dumps(full_flow, indent=2, ensure_ascii=False)}\n</exp_flow>"
    print("--- Machine Code Generation Completed ---")
    return result


@tool
def validate_machine_code(exp_flow_json: str) -> dict:
    """
    Call this tool to perform technical validation on the machine-generated JSON.

    Validation items:
    1. Correctness of JSON format
    2. Validity of resourceId for automated nodes
    3. Completeness of the connection graph (no isolated nodes, no cycles)
    4. Completeness of parameters for external/manual nodes

    Return:
    - status: "success" / "fail"
    - manual_steps: List of node IDs requiring human intervention
    - external_steps: List of node IDs requiring external equipment
    - errors: List of error messages (if failed)

    parameters: exp_flow_json: str
    """
    print("--- Tool: Technical Validation ---")
    is_valid, message, violations = validator_check(exp_flow_json, verbose=True, check_rules=True)
    
    if not is_valid:
        error_code = "VALIDATION_ERROR"
        if "ResourceError" in message:
            error_code = "ResourceError"
        elif "ConnectionError" in message:
            error_code = "ConnectionError"
        elif "SchemaError" in message:
            error_code = "SchemaError"
        elif "RuleError" in message:
            error_code = "RuleError"
        
        return {
            "status": "fail",
            "errors": [{
                "type": "TechnicalError",
                "code": error_code,
                "message": message
            }],
            "rule_violations": [
                {"rule_id": v.rule_id, "severity": v.severity.value, "message": v.message}
                for v in violations
            ] if violations else []
        }
    
    try:
        validator = ProtocolValidator()
        data, _ = validator.extract_json(exp_flow_json)
        
        if data:
            manual_steps = []
            external_steps = []
            
            for node in data.get("nodes", []):
                rid = node.get("resourceId")
                node_id = node.get("templateNodeId")
                
                if rid == -1:
                    params = node.get("parameters", {})
                    op_type = params.get("operationType", "manual")
                    
                    if op_type == "external":
                        external_steps.append({
                            "nodeId": node_id,
                            "deviceName": params.get("deviceName", "External Equipment")
                        })
                    else:
                        manual_steps.append({
                            "nodeId": node_id,
                            "description": params.get("description", params.get("notes", ""))
                        })
            
            nodes = data.get("nodes", [])
            return {
                "status": "success",
                "manual_steps": manual_steps,
                "external_steps": external_steps,
                "rule_warnings": [
                    {"rule_id": v.rule_id, "message": v.message}
                    for v in violations if v.severity.value == "warn"
                ] if violations else [],
                "summary": {
                    "total_nodes": len(nodes),
                    "auto_nodes": len(nodes) - len(manual_steps) - len(external_steps),
                    "external_nodes": len(external_steps),
                    "manual_nodes": len(manual_steps)
                }
            }
    except Exception as e:
        print(f" Post-processing Error: {e}")
    
    return {"status": "success"}


@tool
def fix_machine_code(machine_code: str, errors: str = "", session_id: str = "") -> str:
    """
    Call this tool to repair machine code

    Repair content:
    1. ResourceId errors (invalid IDs mapped to valid IDs or -1)
    2. Missing required params
    3. Format issues (time format, etc.)
    4. Param completion for external/manual nodes
    parameters: machine_code: str, errors: str = "", session_id: str = ""
    """
    print("--- Tool: Intelligent Machine Code Repair ---")
    is_valid, message, fixed_json = fixer_fix(machine_code)
    
    if is_valid:
        print(f"[Success] {message}")
    else:
        print(f"[Error] {message}")
    
    return fixed_json
