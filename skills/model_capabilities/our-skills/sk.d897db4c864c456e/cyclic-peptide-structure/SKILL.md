---
name: cyclic-peptide-structure
description: |
  Predict cyclic-peptide structures and protein-peptide complexes with Boltz, producing
  multiple mmCIF samples and confidence-ready artifacts. Use for a peptide sequence with
  optional target sequence, including head-to-tail cyclic peptides.
license: MIT
metadata:
  skill-author: BioMA
  domain: peptide-design
  status: active
  source: https://github.com/LiorZ/protein-design-skills/tree/main/skills/boltz
routing:
  keywords:
    - cyclic peptide conformer
    - protein peptide complex prediction
    - Boltz
    - peptide structure
  tags:
    - cyclic-peptide
    - conformer
    - protein-peptide-complex
tools:
  - predict_cyclic_peptide_structure

entry_conditions:
  - "A canonical peptide sequence is available."
  - "A target sequence is also available when a complex is requested."

output_expectations:
  - "Exact Boltz YAML and all generated mmCIF sample paths."
  - "Complex predictions are evaluated at the interface, not only by per-chain pLDDT."
  - "Diffusion samples are labeled as model samples, not a Boltzmann ensemble."

procedure:
  - step_id: predict_structure
    title: Predict peptide or complex structures
    instruction: |
      Call predict_cyclic_peptide_structure with only peptide_sequence for the isolated
      peptide, or include target_sequence for a protein-peptide complex. Set cyclic=true
      only for head-to-tail cyclization. Generate multiple diffusion samples.
    recommended_tools: [predict_cyclic_peptide_structure]
    required: true

  - step_id: interpret_samples
    title: Interpret structural samples
    instruction: |
      Rank complex samples with interface confidence such as ipTM and inter-chain PAE
      when available. Do not infer binding affinity from a peptide complex geometry;
      Boltz affinity output is trained for small-molecule ligands, not peptide chains.
    required: true
---

# Cyclic Peptide Structure

This is a compact BioMA adaptation of Lior Zimmerman's MIT-licensed Boltz Skill. The
project-specific tool accepts structured inputs and never exposes a general shell command.
