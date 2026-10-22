---
name: crosslinked-peptide-structure
description: Build a chemically specified cyclic or crosslinked peptide, generate initial coordinates through RDKit and Boltz, and produce a validated xTB-refined conformer ensemble with complete PDB, SDF, CSV, and JSON artifacts.
metadata:
  short-description: Generate crosslinked-peptide conformer ensembles
  version: "1.3.0"
  author: BioMA
tools:
  - build_crosslinked_peptide_topology
  - generate_rdkit_conformer_ensemble
  - generate_boltz_conformer_ensemble
  - optimize_conformer_ensemble_with_xtb
  - validate_cluster_select_conformers
  - export_structure_ensemble
---

# Crosslinked Peptide Structure

Use this workflow for peptide structures defined by a sequence, terminal caps, covalent connections, linker chemistry, pH, experimental conditions, and a requested final ensemble size. Execute the workflow through BioMA's query/tool-calling runtime. The standalone manual dual-route script is a developer smoke test, not the production execution interface.

## Prepare the chemical state

Read [chemical-spec-schema.md](references/chemical-spec-schema.md) and convert the query into one chemical specification.

Resolve the dominant protonation microstate at the supplied pH after applying terminal caps and covalent reactions. List every ionizable site with its state, formal charge, and chemical basis. Treat reaction-defined charges as part of the product topology. Sum the site charges to obtain `total_charge`. Set `uhf` from the closed-shell or open-shell electronic state.

Mark sites with `|pH - pKa| < 1` as microstate-sensitive. Use one microstate per ensemble so every xTB energy is calculated for the same molecular formula and charge. When a noncanonical ionizable group lacks enough information for a reproducible state assignment, return a plain-text clarification question; do not attempt a tool call for it.

Translate the experimental solvent into one xTB implicit-solvent calculation. Prefer ALPB for GFN-xTB geometry optimization. For a mixture, select the main continuous phase or closest supported solvent and record the composition-to-model mapping. Pass the selected model and solvent name to the xTB tool. The xTB tool validates the executable parameters.

## Run nine baseline calls across six tool types

Use one task directory with these stage folders:

1. Call `build_crosslinked_peptide_topology` with the structured chemical specification and `01_topology`.
2. Call `generate_rdkit_conformer_ensemble` with the topology manifest, the user's requested final ensemble size `K` unchanged, and `02_rdkit_raw`. The tool derives the larger raw count internally. For a request of 10 final PDB files, pass `target_ensemble_size=10`, which yields about 200 RDKit conformers for a 16-residue peptide.
3. Call `generate_boltz_conformer_ensemble` with the same topology manifest, unchanged `target_ensemble_size=K`, and `03_boltz_raw`. Unless an explicit budget is supplied, use four seeds and four diffusion samples (at most 16 Boltz proposals). Boltz supplies coordinates only: the RDKit topology remains authoritative for atoms, bonds, caps, formal charges, and stereochemical record.
4. Call `validate_cluster_select_conformers` with the RDKit manifest as `input_manifest_path`, the usable Boltz manifest in `additional_input_manifest_paths_json`, `purpose=select_for_normal`, and `04_select_normal`. This is an intermediate normal-xTB candidate pool, not the final requested ensemble; it is intentionally larger than `K`. Apply global geometry validation, core-heavy-atom RMSD de-duplication, and farthest-point diversity selection. Do not reserve source-specific slots: RDKit and Boltz candidates compete on the same criteria.
5. Call `optimize_conformer_ensemble_with_xtb` with `optimization_level=normal`, the selected solvent settings, the solvent-selection reasoning in `xtb_options_json`, and `05_xtb_normal`.
6. Call `validate_cluster_select_conformers` with `purpose=select_for_tight` and `06_select_tight`.
7. Call `optimize_conformer_ensemble_with_xtb` with `optimization_level=tight`, the same chemical and solvent state, the same solvent-selection reasoning, and `07_xtb_tight`.
8. Call `validate_cluster_select_conformers` with `purpose=select_for_final` and `08_select_final`.
9. Call `export_structure_ensemble` with the final selection manifest and `09_final`. Only this final selection must contain exactly `K` representatives when enough validated RMSD clusters exist.

Read [stage-artifact-contracts.md](references/stage-artifact-contracts.md) when checking manifests or continuing an interrupted xTB stage. Read [decision-and-stopping-rules.md](references/decision-and-stopping-rules.md) when a selection stage requests additional sampling.

## Handle stage results

Continue from a manifest with `status=ok`. Use successful xTB outputs from `partial_success` as input to the next validation call. A Boltz `partial_success` manifest contributes only its projected candidates. A Boltz `failed` manifest means that this task is explicitly degraded to RDKit-only: omit it from merged selection, continue with RDKit, and report `boltz_route_unavailable` in the final result.

Follow `recommended_action` within the sampling limits:

- `generate_more_rdkit_conformers`: generate another RDKit batch with a new `base_seed`, write it to a new stage directory, and repeat normal selection by passing the first manifest as `input_manifest_path` and every additional batch manifest in `additional_input_manifest_paths_json`. Never repeat a completed RDKit configuration hash.
- `inspect_validation_contract`: stop sampling, report the uniformly failed validation check and its manifest, and correct the validation contract before continuing. Additional conformers cannot resolve a validation-contract error.
- `expand_normal_candidates`: preserve the prior normal selection and its completed
  xTB results. If more RDKit sampling is needed, generate a new batch with a new
  `base_seed`. Repeat `select_for_normal` with all raw manifests and pass the previous
  normal-selection manifest as `prior_selection_manifest_path`; the tool excludes
  already selected candidate IDs and returns only the additional candidates needed to
  reach the normal ceiling. Optimize only that additional selection. Then call
  `select_for_tight` with the first normal-xTB manifest as `input_manifest_path` and
  every additional normal-xTB manifest in `additional_input_manifest_paths_json`.
- `expand_tight_candidates`: select and optimize additional tight candidates.
- `validate_available_xtb_results`: run the matching Tool 4 validation stage.
- `revise_chemical_specification`: correct the chemical specification and start a new chemistry task.

Finish with the requested number of representatives from distinct final RMSD clusters. Report the achieved count when the available validated clusters reach the sampling limit.

Treat `RepeatedToolConfiguration` and `RepeatedToolResult` observations as stopping signals for that configuration. Change an effective parameter such as `base_seed`, merge the new manifest with prior sampling manifests, or report the blocking result.

## Runtime selection

Pass the requested Boltz execution settings in `boltz_options_json`. Use `accelerator=cpu` when the visible NVIDIA driver is incompatible with the installed PyTorch CUDA build; the Boltz tool then hides CUDA only from its own subprocess. Use `accelerator=gpu` only after a direct Boltz GPU smoke prediction succeeds. Do not alter the project-wide PyTorch/CUDA installation as part of an ordinary structure-prediction query.

## Present the result

Report:

- complete PDB paths and ensemble SDF path;
- sequence, caps, linker, leaving groups, and covalent connections;
- supplied pH and the site-by-site protonation assignment;
- total formal charge and UHF;
- experimental solvent composition;
- selected xTB method, implicit-solvent model, and solvent name;
- the reasoning that maps experimental conditions to the calculation;
- RDKit raw, normal selection, normal xTB, tight selection, tight xTB, and final candidate counts;
- Boltz raw PDB, projected, rejected, and final-selected candidate counts, including whether the task degraded to RDKit-only;
- convergence, topology, geometry, clash, chirality, clustering, and export checks;
- xTB energies and pairwise core-heavy-atom RMSD values;
- the recorded configuration of experimentally unassigned stereocenters.

Use the SDF and metadata JSON as the complete record for bond orders and formal charges. Use PDB files as coordinate models with explicit connection records.
