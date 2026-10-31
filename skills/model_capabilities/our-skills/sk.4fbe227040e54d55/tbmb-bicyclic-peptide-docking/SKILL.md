---
name: tbmb-bicyclic-peptide-docking
description: |
  Prepare a receptor-only structure and screen a TBMB crosslinked bicyclic-peptide
  conformer ensemble with P2Rank, global DiffDock, restrained AMBER/OpenMM complex
  relaxation, and Vina rescoring. Parameterize the conserved TBMB-Met scaffold from
  reviewed RESP residue templates while leaving standard residues to ff14SB. Use when
  the authoritative ligand graph is supplied as SDF plus TBMB crosslink metadata.
license: MIT
metadata:
  skill-author: BioMA
  domain: structure-based-design
  status: active
routing:
  keywords: [TBMB, bicyclic peptide, DiffDock, P2Rank, Vina, receptor-only docking]
  tags: [TBMB, bicyclic-peptide, docking, P2Rank, DiffDock, OpenMM, AmberTools, Vina]
tools: [parameterize_tbmb_scaffold, prepare_receptor, detect_pockets, diffdock_screen, relax_complexes, vina_refine]
entry_conditions:
  - "A local PDB/mmCIF target and the receptor chain(s) to retain are supplied."
  - "An authoritative TBMB ensemble SDF, a matching named reference PDB, and crosslink metadata JSON are supplied."
  - "A qualified reviewer has approved a source manifest containing converged multi-conformer RESP charges, MSU/CMU/TMB templates (plus NMU when required), reviewed TBMB-Met crosslink bonded terms, and reviewed ff14SB/non-standard peptide-boundary terms."
  - "P2Rank, DiffDock, AmberTools, Vina, and Meeko command templates are configured in the query; OpenMM is importable in the execution environment."
output_expectations:
  - "Receptor-only PDB and deletion/repair audit."
  - "Up to five P2Rank pocket hypotheses, a configurable DiffDock pre-relaxation pool, five physically relaxed complexes, and three Vina-ranked poses with linked evidence."
  - "Rejected poses with an explicit reason; no fabricated candidate is used to meet a requested count."
procedure:
  - step_id: parameterize
    title: Seal the reusable TBMB-Met parameter module
    instruction: "Call parameterize_tbmb_scaffold. It must accept only a reviewer-approved source manifest with converged multi-conformer RESP charges and reviewed crosslink bonded terms, copy the residue templates/FRCMOD into a SHA-256-bound bundle, and pass a reference-sequence TLEaP assembly and +5 charge test. It does not perform QM or make an incomplete source valid. Never generate whole-peptide AM1-BCC or substitute Gasteiger/fast charges. The resulting TBMB-Met module may be reused only when local crosslink chemistry is unchanged; every new sequence still requires its own TLEaP assembly and validation."
    recommended_tools: [parameterize_tbmb_scaffold]
    required: true
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
    instruction: "Call diffdock_screen. Ensemble SDF and metadata are authoritative for atom order, bond graph, +5 charge, and all three TBMB crosslinks. DiffDock is global, then each pose is assigned to its nearest P2Rank pocket. Before relaxation reject only chemistry failures, failed sentinels, poses outside the configured pocket range, and gross overlaps; do not reject a repairable <1.5 A interface contact."
    recommended_tools: [diffdock_screen]
    required: true
  - step_id: restrained_relaxation
    title: Restrained complex relaxation
    instruction: "Call relax_complexes. It must validate the schema-v2 reusable TBMB-Met bundle, load ff14SB for standard residues plus MSU/NMU/CMU/TMB templates and the reviewed crosslink/peptide-boundary FRCMODs, rename the current candidate by the named reference PDB, explicitly build all three SD-CM bonds, and create sequence-specific ligand/complex prmtop and inpcrd files. Map relaxed coordinates by residue number and atom name rather than assuming SDF/TLEaP atom order. Stop if bundle provenance, charge, atom mapping, crosslink, FRCMOD, TLEaP, energy/force, topology, displacement, clash, or pocket-retention checks fail. Rank only candidates passing post-relaxation gates."
    recommended_tools: [relax_complexes]
    required: true
  - step_id: local_refinement
    title: Vina rescoring and local refinement
    instruction: "Call vina_refine only if the post-relaxation shortlist is nonempty. Use each candidate's own relaxed receptor and ligand. Run score-only on the relaxed pose and --local_only in a pose-centered box that encloses the complete peptide; do not initiate a global Vina search. Treat the OpenMM-relaxed complex as the recommended structure unless the local Vina pose is mapped back to the authoritative graph and revalidated."
    recommended_tools: [vina_refine]
    required: true
  - step_id: interpretation
    title: Interpret limited evidence
    instruction: "For each Vina-ranked candidate, report the linked DiffDock pose/confidence, pocket assignment, parameterization audit, sequence-specific ligand/complex prmtop and inpcrd, initial/final OpenMM energy, final restrained RMS force, receptor/ligand RMSD, authoritative-TBMB topology, +5 charge, covalent-bond geometry, pre/post-relaxation clash/contact counts, minimum receptor distance, pocket-centroid distance, Vina score-only and local-only scores. Do not separate these facts into unlinked summaries. Visually inspect selected interfaces. This skill ends before MD, MM/GBSA, or binding-affinity claims."
    required: true
---

# TBMB Bicyclic-Peptide Docking

This skill is a non-MD screening workflow. It must never reconstruct the TBMB molecular
graph from PDB/PDBQT, silently accept missing AMBER parameters, or relax charge, topology,
covalent-bond geometry, post-relaxation clash, displacement, or pocket-distance gates.
The source gate and parameter-bundle validator establish identity, completeness, provenance,
and successful assembly; they do not prove that the quantum-chemical derivation is correct.
A qualified reviewer must approve that derivation before this workflow is used. The custom
module is scoped to unchanged TBMB-Met chemistry, while ff14SB supplies ordinary residues and
TLEaP assembles each sequence separately. If the approved source or bundle is absent, stop
rather than generating whole-peptide AM1-BCC charges or using a fast-charge fallback. Outputs
remain screening evidence and are not MD, MM/GBSA, or affinity estimates.
