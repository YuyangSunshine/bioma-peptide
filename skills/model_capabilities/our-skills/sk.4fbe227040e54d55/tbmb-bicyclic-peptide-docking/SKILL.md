---
name: tbmb-bicyclic-peptide-docking
description: |
  Prepare a receptor-only structure and screen a TBMB crosslinked bicyclic-peptide
  conformer ensemble with P2Rank, global DiffDock, and Vina local refinement. Use when
  the authoritative ligand graph is supplied as SDF plus TBMB crosslink metadata.
license: MIT
metadata:
  skill-author: BioMA
  domain: structure-based-design
  status: active
routing:
  keywords: [TBMB, bicyclic peptide, DiffDock, P2Rank, Vina, receptor-only docking]
  tags: [TBMB, bicyclic-peptide, docking, P2Rank, DiffDock, Vina]
tools: [prepare_receptor, detect_pockets, diffdock_screen, vina_refine]
entry_conditions:
  - "A local PDB/mmCIF target and the receptor chain(s) to retain are supplied."
  - "An authoritative TBMB ensemble SDF and crosslink metadata JSON are supplied."
  - "P2Rank, DiffDock, Vina, and Meeko command templates are configured in the query."
output_expectations:
  - "Receptor-only PDB and deletion/repair audit."
  - "Up to five P2Rank pocket hypotheses, five DiffDock-screened poses, and three Vina-ranked poses, each retaining its linked DiffDock confidence and hard-gate evidence."
  - "Rejected poses with an explicit reason; no fabricated candidate is used to meet a requested count."
procedure:
  - step_id: prepare
    title: Prepare the receptor only
    instruction: "Call prepare_receptor. Retain only the requested polymer chain(s); do not retain a crystallographic ligand, buffer, Fab, water, ion, or glycan unless the query explicitly changes the policy."
    recommended_tools: [prepare_receptor]
    required: true
  - step_id: pockets
    title: Detect pockets
    instruction: "Call detect_pockets. Interpret P2Rank pockets as hypotheses. If fewer than requested pockets pass the cutoff, report that fact rather than lowering the threshold silently. A user may deliberately set a lower cutoff in the query when the aim is exploratory peptide docking rather than high-druggability small-molecule screening."
    recommended_tools: [detect_pockets]
    required: true
  - step_id: global_screen
    title: Global TBMB screening
    instruction: "Call diffdock_screen. Ensemble SDF and metadata are authoritative for atom order, bond graph, +5 charge, and all three TBMB crosslinks. DiffDock is global, then each pose is assigned to its nearest P2Rank pocket."
    recommended_tools: [diffdock_screen]
    required: true
  - step_id: local_refinement
    title: Local Vina refinement
    instruction: "Call vina_refine only if the shortlist is nonempty. Vina must run --local_only in a pose-centered box that encloses the complete peptide; do not initiate an expensive global Vina search for this high-DOF peptide."
    recommended_tools: [vina_refine]
    required: true
  - step_id: interpretation
    title: Interpret limited evidence
    instruction: "For each Vina-ranked candidate, report the linked DiffDock pose/confidence, pocket assignment, authoritative-TBMB topology result, +5 charge result, covalent-bond geometry, clash/contact counts, minimum receptor distance, pocket-centroid distance, and Vina score. Do not separate these facts into unlinked summaries. Visually inspect selected interfaces. This skill ends before MD, MM/GBSA, or binding-affinity claims."
    required: true
---

# TBMB Bicyclic-Peptide Docking

This skill is a non-MD screening workflow. It must never reconstruct the TBMB molecular
graph from PDB/PDBQT, or silently relax charge, topology, covalent-bond geometry, clash,
or pocket-distance gates.
