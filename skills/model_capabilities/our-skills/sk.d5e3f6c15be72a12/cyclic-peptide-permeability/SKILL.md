---
name: cyclic-peptide-permeability
description: |
  Predict cyclic-peptide membrane permeability with the CPMP model from peptide SMILES,
  returning PAMPA, Caco-2, RRCK, and MDCK outputs. Use for developability screening of
  cyclic peptides, not generic protein solubility or small-molecule Lipinski filtering.
license: Apache-2.0
metadata:
  skill-author: BioMA
  domain: peptide-developability
  status: active
  source: https://github.com/panda1103/CPMP
routing:
  keywords:
    - cyclic peptide membrane permeability
    - CPMP
    - PAMPA
    - Caco-2
    - RRCK
    - MDCK
  tags:
    - cyclic-peptide
    - permeability
    - developability
tools:
  - predict_cyclic_peptide_permeability

entry_conditions:
  - "Canonical cyclic-peptide SMILES are available."
  - "BIOMA_CPMP_ROOT points to an Apache-2.0 CPMP checkout with its released weights."

output_expectations:
  - "One PAMPA, Caco-2, RRCK, and MDCK prediction per input SMILES."
  - "Predictions are labeled as CPMP model outputs rather than experimental measurements."

procedure:
  - step_id: predict_permeability
    title: Run CPMP endpoints
    instruction: |
      Call predict_cyclic_peptide_permeability once for the candidate batch. Preserve
      input order and retain the generated CSV artifacts for reproducibility.
    recommended_tools: [predict_cyclic_peptide_permeability]
    required: true

  - step_id: interpret_scope
    title: Interpret within model scope
    instruction: |
      Compare candidates within the same endpoint and model version. Do not treat a
      predicted value as a measured permeability or mix endpoint scales without an
      explicit normalization method.
    required: true
---

# Cyclic Peptide Permeability

This BioMA Skill wraps the official Apache-2.0 CPMP inference workflow. The model checkout
and released weights remain external; BioMA stores only the adapter and reproducible CSVs.
