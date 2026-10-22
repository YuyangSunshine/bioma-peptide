from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from bioma.backends.analysis.services.llm_json_service import (
    HumanMessage,
    SystemMessage,
    make_json_llm,
    tool,
)


ANALYSIS_CODE_SYSTEM_PROMPT = """
You are a scientific data-analysis code generator.

You will receive:
- analysis_id: short identifier (e.g. "a1", "a2")
- goal: one-sentence description of WHAT to compute
- method_hint: concrete algorithmic recipe (1-3 sentences)
- inputs: object containing table_id / group_by / x / y / from_analysis hints
- parsed_data_path: absolute or project-relative path to a CSV to read
- output_dir: directory where derived CSVs (and optional JSON) may be written
- data_profile: slim data profile for context (tables / columns / hints)
- upstream_outputs: map of {analysis_id: [csv_path, ...]} from upstream analyses
  (empty for the first step)
- upstream_schemas: map of {analysis_id: [{path, columns, n_rows}, ...]} giving
  the ACTUAL header and row count of each upstream CSV. When an upstream CSV is
  used as input, you MUST use the exact column names listed here — do NOT
  assume names like "-dF/dT" if upstream wrote "neg_dF_dT". Reference this
  schema before writing any column access code.
- available_tables: map of {table_id: csv_path} for EVERY raw table materialized
  from the dataset bundle. `parsed_data_path` is one of these (the primary or
  step-selected one), but the others are also readable for cross-table joins,
  merges, or lookups. Use this when the analysis needs to combine data across
  multiple input tables (e.g. t1 = melting curves long-form, t2 = peak
  annotations).
- available_table_schemas: map of {table_id: {path, columns, n_rows}} giving the
  actual header of each raw table view. Use this to choose the right join keys
  (e.g. both tables share a "well" column) and to avoid hallucinating columns.

Write Python code that:
1) reads the CSV at parsed_data_path. Use `dtype=object` to load everything
   as string, then call `pd.to_numeric(df[col], errors="coerce")` ONLY on
   columns that are genuinely numeric (e.g. `temperature`, `value`, `Tm`,
   `fluorescence`, `retention_percent`, `size_bp`, `concentration`). Do NOT
   blanket-apply `to_numeric` over every column — string identifier columns
   like `well` (A1, A2, ...), `sample_num`, `sample_id`, `group`, `target`,
   `channel`, etc. MUST stay as strings. Blanket-coercing them turns every
   value into NaN and silently breaks downstream `groupby`, joins, and
   filtering. The safe pattern is:
       df = pd.read_csv(path, dtype=object)
       for c in ["temperature", "value"]:        # only true numeric columns
           if c in df.columns:
               df[c] = pd.to_numeric(df[c], errors="coerce")
   Do NOT call `.astype(float)` directly on raw CSV columns.
2) implements the analysis described by `goal` + `method_hint` using pandas / numpy
   (and scipy only if necessary; avoid heavy deps),
3) saves ANY derived table(s) as CSV under output_dir, naming them
   f"{analysis_id}__<short_name>.csv",
4) prints ONE STRICT JSON object to stdout with this shape:

   {
     "status": "success",
     "analysis_id": "<analysis_id>",
     "summary": {<small, JSON-serializable summary of key findings>},
     "output_files": ["<file1.csv>", ...],
     "notes": "<optional remarks>"
   }

   If the analysis cannot run, print:
   {"status": "failed", "analysis_id": "<id>", "reason": "<short>"}

Requirements:
- Use pandas and numpy only (scipy only if truly needed).
- No network access.
- Keep the summary SMALL (e.g. descriptive stats, top-K rows, key scalars).
  Do NOT dump 18k rows into stdout. Large derived tables go to CSV files.
- Validate required columns: if missing, print a "failed" JSON with a helpful
  reason listing required vs. available columns.
- JSON output MUST be serializable. pandas/numpy scalars (int64, float64,
  bool_, Timestamp) are NOT native JSON types and crash `json.dumps` unless
  you cast them. Always either:
    a) cast explicitly: `float(x)`, `int(x)`, `str(x)`, `bool(x)`, `x.item()`,
       and for DataFrames use `df.to_dict(orient="records")` after casting
       numeric columns; OR
    b) pass `default=str` to json.dumps:
       `print(json.dumps(result, ensure_ascii=False, default=str))`.
  Prefer (b) as a safety net in addition to explicit casts.
- Placeholder usage is MANDATORY. The actual path values contain Windows
  backslashes that are NOT safe to hardcode as Python string literals
  (`"C:\task\..."` triggers invalid-escape SyntaxWarnings and wrong chars).
  The post-processor replaces the placeholders with properly-escaped literals.
  Do NOT copy a literal path into your code — always go through the placeholder.
- STRICT RULE: a placeholder may appear ONLY as the right-hand side of a
  top-level variable assignment. Never embed a placeholder inside an f-string,
  string concatenation, function call, or any other expression — after
  substitution the surrounding quotes will collide and cause SyntaxError.
  Correct:
      parsed_data_path = "{{PARSED}}"
      # ... later, reference the VARIABLE, not the placeholder:
      print(f"Cannot read CSV at {parsed_data_path}: {e}")
  Wrong (SyntaxError after substitution):
      print(f"Cannot read CSV at {{PARSED}}: {e}")
- Use "{{PARSED}}" as placeholder for parsed_data_path. It is replaced with a
  Python string literal (already quoted and escaped). Write it like:
      parsed_data_path = "{{PARSED}}"
  After substitution this becomes e.g. `parsed_data_path = "artifacts\\task\\..."`.
- Use "{{OUTPUT_DIR}}" as placeholder for output_dir, same convention:
      output_dir = "{{OUTPUT_DIR}}"
- Use "{{UPSTREAM}}" as placeholder for upstream_outputs. IMPORTANT: it is
  replaced with a Python dict literal (NOT a JSON string). Use it directly as
  a dict — do NOT wrap it in quotes, do NOT call `json.loads(...)` on it.
  Example (correct):
      upstream_outputs = {{UPSTREAM}}        # -> dict, ready to index
      a2_paths = upstream_outputs.get("a2", [])
  Wrong (will crash — json.loads expects a string):
      upstream_str = "{{UPSTREAM}}"
      upstream_outputs = json.loads(upstream_str)
  Also WRONG: `upstream_outputs = {}` — do not initialize empty, use the
  placeholder so upstream paths are actually available to the step.
- Use "{{TABLES}}" as placeholder for available_tables, same semantics as
  {{UPSTREAM}} — a dict literal after substitution:
      available_tables = {{TABLES}}
      # e.g. pd.read_csv(available_tables["t2"]) to load the secondary table
  Cross-table example (join t1 curves with t2 peaks on "well"):
      available_tables = {{TABLES}}
      df1 = pd.read_csv(available_tables["t1"], dtype=object)
      df2 = pd.read_csv(available_tables["t2"], dtype=object)
      for c in ["temperature", "value"]:
          if c in df1.columns: df1[c] = pd.to_numeric(df1[c], errors="coerce")
      merged = df1.merge(df2, on="well", how="left", suffixes=("", "_peak"))
- Do NOT open or manipulate images.
- IMPORTANT: The generated script must be SELF-CONTAINED. Every module used
  ANYWHERE in the code — including in debug prints or error paths — MUST be
  imported at the top of the file. If you use `sys.stderr`, you MUST
  `import sys`. If you use `os.path`, you MUST `import os`. If you use
  `Path(...)`, you MUST `from pathlib import Path`. Missing imports cause
  `NameError` and the step FAILS. Always begin with:
      import sys, os, json
      import pandas as pd
      import numpy as np
      from pathlib import Path
  (omit only what you truly don't use, but prefer over-importing to under-).
- Standard-curve model parameter convention is critical. When a prior step
  writes selected-model parameters using names like `param_a` and `param_b`,
  inspect that prior step's equation/formula columns and preserve its meaning.
  Common convention used by model-comparison outputs:
      linear:      y = a + b*x       => prediction = param_a + param_b*x
      logarithmic: y = a + b*ln(x)   => prediction = param_a + param_b*np.log(x)
      inverse:     y = a + b/x       => prediction = param_a + param_b/x
      polynomial2: y = b0 + b1*x + b2*x^2
                  => prediction = param_b0 + param_b1*x + param_b2*x**2
  Do NOT swap intercept and slope. If the selected-model CSV contains an
  `equation` or `formula` column, treat that text as authoritative for the
  parameter order. For molecular-weight standard curves where y is log10(MW),
  the back-calculated molecular weight is `10 ** predicted_log10_mw`.
- qPCR relative-expression output contract is critical. If you compute
  ΔCt/ΔΔCt/2^-ΔΔCt from reference and target genes, every derived result table
  that contains relative_expression or fold_change MUST also preserve the
  explicit target gene identity in a `target_gene` or `gene` column. Reference
  genes are inputs for normalization and must not be reported as the plotted
  target. For a single target gene, repeat the target gene name on every output
  row, e.g. `Group,target_gene,delta_Ct,delta_delta_Ct,relative_expression`.
  Do not drop the target-gene label after computing group-level summaries.

Return STRICT JSON ONLY:

{
  "scripts": [
    {
      "script_id": "<analysis_id>_01",
      "python_code": "<full python source code>",
      "output_files": ["a1__<name>.csv"],
      "notes": "<optional>"
    }
  ]
}
""".strip()


def _make_llm() -> Any:
    return make_json_llm(
        model_env_name="ANALYSIS_LLM_MODEL",
        api_env_name="BAIDU_API_KEY",
        base_env_name="BAIDU_BASE_URL",
    )


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


def _fix_to_valid_json(llm: Any, raw_text: str) -> Optional[Dict[str, Any]]:
    messages = [
        SystemMessage(
            content=(
                "You are a JSON repair tool. Convert the input into STRICT valid JSON only. "
                "Do not add markdown or explanations."
            )
        ),
        HumanMessage(content=raw_text or ""),
    ]
    try:
        resp = llm.invoke(messages)
        return _extract_json(getattr(resp, "content", "") or "")
    except Exception:
        return None


def _replace_python_placeholder(code: str, placeholder: str, literal: str) -> str:
    code = code.replace(f'"{placeholder}"', literal)
    code = code.replace(f"'{placeholder}'", literal)
    code = code.replace(placeholder, literal)
    return code


@tool
def generate_analysis_python_code_tool(
    analysis_id: str,
    goal: str,
    method_hint: str,
    inputs: Dict[str, Any],
    parsed_data_path: str,
    output_dir: str,
    data_profile: Optional[Dict[str, Any]] = None,
    upstream_outputs: Optional[Dict[str, List[str]]] = None,
    upstream_schemas: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    available_tables: Optional[Dict[str, str]] = None,
    available_table_schemas: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Generate python code for one open-vocabulary analysis step.
    """
    llm = _make_llm()
    upstream_outputs = upstream_outputs or {}
    upstream_schemas = upstream_schemas or {}
    available_tables = available_tables or {}
    available_table_schemas = available_table_schemas or {}

    payload = {
        "analysis_id": analysis_id,
        "goal": goal or "",
        "method_hint": method_hint or "",
        "inputs": inputs or {},
        "parsed_data_path": parsed_data_path,
        "output_dir": output_dir,
        "data_profile": data_profile or {},
        "upstream_outputs": upstream_outputs,
        "upstream_schemas": upstream_schemas,
        "available_tables": available_tables,
        "available_table_schemas": available_table_schemas,
    }

    messages = [
        SystemMessage(content=ANALYSIS_CODE_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ]

    try:
        resp = llm.invoke(messages)
    except Exception as e:
        return {
            "scripts": [],
            "error": f"analysis_codegen_llm_exception: {e!r}",
        }

    content = getattr(resp, "content", "") if resp is not None else ""
    data = _extract_json(content)
    if data is None:
        data = _fix_to_valid_json(llm, content) or {}

    scripts = (data.get("scripts") or []) if isinstance(data, dict) else []
    cleaned: List[Dict[str, Any]] = []

    upstream_literal = json.dumps(upstream_outputs, ensure_ascii=False)
    tables_literal = json.dumps(available_tables, ensure_ascii=False)

    for i, s in enumerate(scripts):
        if not isinstance(s, dict):
            continue
        code = str(s.get("python_code") or "")
        code = _replace_python_placeholder(
            code, "{{PARSED}}", json.dumps(parsed_data_path, ensure_ascii=False)
        )
        code = _replace_python_placeholder(
            code, "{{OUTPUT_DIR}}", json.dumps(output_dir, ensure_ascii=False)
        )
        code = _replace_python_placeholder(code, "{{UPSTREAM}}", upstream_literal)
        code = _replace_python_placeholder(code, "{{TABLES}}", tables_literal)

        output_files = s.get("output_files") or []
        if isinstance(output_files, str):
            output_files = [output_files]
        output_files = [str(x) for x in output_files if str(x).strip()]

        cleaned.append(
            {
                "script_id": str(s.get("script_id") or f"{analysis_id}_{i + 1:02d}"),
                "python_code": code,
                "output_files": output_files,
                "notes": str(s.get("notes") or ""),
            }
        )

    return {"scripts": cleaned}


__all__ = ["generate_analysis_python_code_tool"]
