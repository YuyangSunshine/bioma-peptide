"""Device alignment and machine-code generation prompts."""
from bioma.backends.automation.registry.device_registry import get_device_prompt_content


ALIGNMENT_PROMPT = """
You are a biological experiment automation expert, responsible for converting any biological experimental protocol into an executable standardized workflow.

Core Task
Convert the experimental protocol in [Reference Information] into a **step-by-step standardized workflow**.
You need to label the execution method for each step, so the system knows which steps can be automated, which require manual intervention, or which need external equipment.


## 📌 Step Labeling Rules (Important!)
Each step must be labeled with one of the three types below:

| Label         | Meaning                  | Usage Scenario                                      |
|---------------|--------------------------|-----------------------------------------------------|
| **[AUTO]**    | Executable by current automation platform | Use equipment from the [Available Automation Devices] below |
| **[EXTERNAL]**| Requires external equipment | The lab may have this device, but it’s not on the automation platform |
| **[MANUAL]**  | Requires manual operation | No equipment available, needs manual judgment, or involves special operations |

**Important:** [EXTERNAL] and [MANUAL] are not "failures" or "abnormalities"—they are normal components of the experimental workflow.
Complex biological experiments often require the coordination of multiple devices and manual operations, so label them truthfully.

---

{device_summary}

---

{external_guide}

---

{manual_guide}

---

{consumable_guide}

---

## 📋 Output Format Specifications

### Step Numbering Rules
- Main steps: 1, 2, 3...
- Sub-steps: 1.1, 1.2, 1.3... (continuous operations on the same device)


### Step Format
[Label] Device Category/Operation Type Operation Description (Key Parameters)
- Consumables: Required consumable type
- Reagents: Reagent names and dosages
- Output: Product of this step
- Notes: Special precautions (optional)


### Example Output
1. [AUTO] Pipetting Station Build PCR reaction system
   1.1 [AUTO] Pipetting Station Add template DNA (volume=2μL)
      - Reagents: Template DNA 2μL/well
      - Consumables: PCR plate, pipette tips
   1.2 [AUTO] Pipetting Station Add primers and enzyme
      - Reagents: Forward primer 1μL, reverse primer 1μL, Taq enzyme 0.25μL
   1.3 [AUTO] Pipetting Station Add PCR premix and mix (mixing time=30 seconds)
      - Reagents: 2×PCR Master Mix 25μL
      - Output: 50μL PCR reaction system

2. [AUTO] Sealer Seal membrane (temperature=170°C)
   - Consumables: PCR sealing film

3. [AUTO] PCR Instrument PCR amplification
   - Program: 95°C 5min → [95°C 30s → 58°C 30s → 72°C 1min] ×35 → 72°C 10min → 4°C hold
   - Output: PCR amplification product

4. [AUTO] Peeler Peel membrane

5. [EXTERNAL] Gel Electrophoresis System Agarose gel electrophoresis to detect PCR products
   - Conditions: 1.5% agarose gel, 120V, 30min
   - Reagents: DNA Marker, nucleic acid stain
   - Output: Electrophoresis profile
   - Notes: Requires external gel electrophoresis equipment and imaging system

6. [MANUAL] Manual Judgment Determine if PCR is successful based on electrophoresis results
   - Judgment criteria: Target band size ~500bp, no non-specific amplification
   - Output: PCR success/failure judgment

7. [EXTERNAL] Gel Extraction System Cut gel to recover target fragment (if PCR is successful)
   - Reagents: Gel extraction kit
   - Output: Purified DNA fragment


---

## 🚨 Important Rules
1. **Completeness**: Do not omit any experimental steps, even if they involve external equipment or manual operations
2. **Sealing Rule**: Plates must be sealed before entering the PCR instrument/incubator; peel the seal after the operation is complete
3. **Parameter Extraction**: Extract specific parameters from the reference information; use standard values in the field if no clear value is provided
4. **Pipetting Merging**: Continuous pipetting operations on the same device should be represented as sub-steps
5. **Output Labeling**: Label the product of each step to facilitate tracking of the experimental workflow
6. **Truthful Labeling**: Label steps as [AUTO]/[EXTERNAL]/[MANUAL] based on actual device capabilities; do not force all steps to be marked as [AUTO]


---

## 📥 Input Information

### User Requirements
{exp_info}

### Reference Information (Experimental Protocol)
{protocol}

### Reference Documents (If Any)
{document}
---

## 📤 Output Requirements
Directly output the standardized experimental steps.
- Each step must include a [AUTO]/[EXTERNAL]/[MANUAL] label
- Fully cover all steps of the experiment; do not omit any
- External equipment and manual operations are normal—label them truthfully
"""


def build_alignment_prompt(exp_info: str, protocol: str, document: str = "") -> str:
    """
    Build the complete alignment Prompt
    
    Args:
        exp_info: User's experimental requirement information
        protocol: Draft of the scientific experimental protocol
        document: Reference document content (optional)
    
    Returns:
        Complete Prompt string
    """
    return ALIGNMENT_PROMPT.format(
        device_summary=generate_device_capability_summary(),
        external_guide=generate_external_device_guide(),
        manual_guide=generate_manual_operation_guide(),
        consumable_guide=generate_consumable_category_guide(),
        exp_info=exp_info if exp_info else "No special requirements",
        protocol=protocol if protocol else "None",
        document=document if document else "None"
    )
   
# ==================== Phase 2: Code Generation ====================


SIMPLIFIED_PAINT_PROMPT = """
You are a biological automation code generation expert. Convert standardized experimental steps into a machine-executable JSON structure.

## 🎯 Task
Convert experimental steps into a node sequence. Each node must include:
1. **resourceId**: Device ID (for automated devices) or -1 (for external/manual operations)
2. **action**: Operation description
3. **keyParams**: Critical parameters (only fill in experiment-specific values; the system will supplement the rest)


---
{device_content}
---

## 🌐 External Equipment & Manual Operations
**All [EXTERNAL] and [MANUAL] steps use resourceId = -1 uniformly**

keyParams must include:
- `operationType`: "external" or "manual"
- `deviceName`: Device name (for external) or "manual operation" (for manual)
- `description`: Detailed operation description

---

## 📋 Output Format

```json
{{
  "templateName": "Experiment Name",
  "description": "Experiment Description",
  "nodes": [
    {{"templateNodeId": 1, "resourceId": 59, "action": "Pipetting", "keyParams": {{...}}}},
    {{"templateNodeId": 2, "resourceId": -1, "action": "External Operation", "keyParams": {{"operationType": "external", ...}}}}
  ],
  "connections": [
    {{"sourceNodeId": 1, "targetNodeId": 2}}
  ]
}}

## ⚠️ Key Rules
- resourceId must be selected from the table above; use -1 for external/manual operations
- keyParams only needs to fill in key parameters; the system will automatically complete the rest
- connections must cover all nodes and form a directed acyclic graph


## 📥 Input
**Experiment Steps**
{protocol}

**User Information**
{exp_info}

## 📤 Output
Only output the JSON, enclosed between <exp_flow> and </exp_flow>.
Ensure all steps are included, including external devices and manual operations.
"""


def build_paint_prompt(protocol: str, exp_info: str, suggestion: str = "") -> str:
    """Construct code generation Prompt"""
    device_content = get_device_prompt_content()
    prompt = SIMPLIFIED_PAINT_PROMPT.format(
        device_content=device_content,
        protocol=protocol if protocol else "None",
        exp_info=exp_info if exp_info else "None"
    )

    if suggestion and suggestion != "[N/A]":
        prompt += f"\n\n## Repair Suggestions\n{suggestion}"

    return prompt 




"""
Prompt auxiliary function - generates dynamic content such as device capability summaries
"""

def generate_device_capability_summary() -> str:
    """Generate automation device capability summary"""
    return """
## 🔧 Currently Available Automation Devices

The following devices have been integrated into the automation platform and can be marked as [AUTO]:

| Device Category | Core Capabilities | Applicable Operations |
|---------|---------|---------|
| **Pipetting Workstation** | Liquid transfer (0.5μL-1mL), multi-channel pipetting, mixing, temperature control (4-95°C), magnetic bead separation | Sample loading, aliquoting, serial dilution, magnetic bead purification, plate transfer |
| **PCR Instrument** | Precise temperature cycling (4-99°C), constant-temperature incubation | PCR amplification, RT-PCR, restriction digestion, ligation, heat shock |
| **Fluorescent Quantitative PCR Instrument** | Real-time fluorescence detection, melting curve analysis | qPCR, gene expression analysis |
| **Sealing Machine** | Heat sealing | Plate sealing (required before PCR instrument/incubator) |
| **De-sealing Machine** | Automatic membrane peeling | Remove sealed membrane |
| **Centrifuge** | Centrifugation (500-15000×g) | Cell sedimentation, DNA precipitation, phase separation |
| **Incubator** | Constant-temperature (20-60°C) shaking culture | Bacterial culture, enzyme reaction, hybridization |
| **Microplate Reader** | Absorbance/fluorescence/luminescence detection | OD measurement, ELISA, fluorescent quantification, reporter gene |
| **Plate Washer** | Automatic microplate washing | ELISA plate washing, cell washing |
| **Capillary Electrophoresis Instrument** | Fragment analysis, sequencing | DNA/RNA fragment detection, quality control |
| **Refrigerator** | Low-temperature storage (4°C) | Sample storage, enzyme preservation |
| **Colony Picker** | Colony identification and picking | High-throughput colony screening
 |
"""
		
def generate_external_device_guide() -> str:
    """Generate external device usage guide"""
    return """
## 🌐 External Device Labeling Guide

**Any device not on the automation platform** should be marked as [EXTERNAL], and the device name should be noted.This is a normal part of the experimental workflow, not an "error" or "abnormality".

### Common External Device Examples (not limited to these)

**Molecular Biology**: Gel electrophoresis system, gel imager, electroporator, nucleic acid extractor, sequencer...
**Cell Biology**: Flow cytometer, microscope (inverted/fluorescent/confocal), CO₂ incubator, cell counter...
**Protein-related**: Western Blot system, protein purification system (FPLC/HPLC), mass spectrometer, ultrasonic disruptor...
**Microbiology-related**: Fermenter, anaerobic workstation, strain preservation system...
**General Equipment**: Balance, pH meter, spectrophotometer, autoclave, water bath...
"""


def generate_manual_operation_guide() -> str:
    """Generate manual operation labeling guide"""
    return """
## ✋ Manual Operation Labeling Guide

The following situations should be marked as [MANUAL]:

| Scenario Category       | Examples                                   |
|-------------------------|--------------------------------------------|
| **Requires Human Judgment** | Observe color changes, assess cell status, select clones, quality confirmation |
| **Precision Operations**     | Microinjection, tissue sectioning, manual picking, precision weighing |
| **Safety-Related**           | Hazardous chemical handling, radioactive material operation |
| **Special Operations**       | Operational steps without corresponding equipment |
| **Decision Points**          | Determine subsequent operations based on previous step results |

Manual operation is a normal part of human-machine collaboration; please label truthfully.
"""


def generate_consumable_category_guide() -> str:
    """Generate Consumable Category Guide"""
    return """
## 📦 Available Consumable Categories

| Consumable Category | Usage | Volume Range |
|---------------------|-------|--------------|
| **PCR Plate** | PCR reaction, qPCR | 96-well/384-well, ≤100μL/well |
| **Deep Well Plate** | Sample storage, large-volume reactions | 96-well, 1.1-2.2mL/well |
| **Microplate** | For detection (transparent/black/white) | 96-well, ≤300μL/well |
| **Reagent Reservoir** | Bulk reagent storage | 12-290mL |
| **Pipette Tip** | Liquid transfer | 90μL/230μL/1070μL |

Note: The specific consumable model will be automatically matched by the system; there is no need to specify it in the protocol.
"""
