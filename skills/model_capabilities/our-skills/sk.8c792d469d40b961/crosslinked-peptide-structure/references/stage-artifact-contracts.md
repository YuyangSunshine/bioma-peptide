# Stage artifact contracts

Every stage returns a compact JSON response containing `status`, `manifest_path`, counts, and the principal artifact path.

Every manifest carries:

```text
contract_version
tool_name
tool_version
stage
status
chemical_hash
input_hash
config_hash
result_hash
parent_manifest
parent_manifests
topology_manifest
candidate_count
candidate_ids
artifacts
warnings
recommended_action
```

`input_hash` identifies the normalized upstream configuration set. `config_hash`
identifies the effective configuration of the current tool call. `result_hash`
identifies the path-independent scientific outcome. Coordinate-bearing stages also
record `artifact_hashes.ensemble_scientific_sha256`.

## Stage outputs

| Stage | Principal artifacts |
|---|---|
| topology | `topology.sdf`, `chemical_spec.json`, `atom_map.json`, `topology_validation.json` |
| RDKit raw | `rdkit_raw_conformers.sdf`, `rdkit_embedding_records.csv` |
| normal selection | selected SDF, validation JSON, RMSD CSV, cluster JSON, selection CSV |
| normal xTB | optimized SDF, result JSON, execution plan, checkpoint, per-conformer logs |
| tight selection | selected SDF, validation JSON, RMSD CSV, cluster JSON, selection CSV |
| tight xTB | optimized SDF, result JSON, execution plan, checkpoint, per-conformer logs |
| final selection | selected SDF, validation JSON, RMSD CSV, cluster JSON, selection CSV |
| final export | per-conformer PDB, ensemble SDF, summary CSV, RMSD CSV, metadata JSON, export validation |

## Hash boundaries

- `chemical_hash` covers sequence, caps, components, connections, pH-derived charge states, total charge, and UHF.
- `result_hash` covers the scientific status and candidate result while excluding output-directory names, allowing PredAgent to recognize a repeated completed result.
- Every coordinate-bearing stage includes a path-independent scientific ensemble hash
  in its result hash. Different RDKit batches or optimized/selected/exported coordinates
  therefore cannot collide merely because they reuse candidate names.
- RDKit generation config covers the input topology hash, seed plan, target count, and RDKit version.
- Selection config covers every parent-manifest hash so adding a new RDKit sampling batch creates a distinct selection task.
- xTB config covers the input manifest hash, method, solvent model, solvent, charge, UHF, optimization level, cycles, executable, and tool version.
- selection config covers the input manifest hash, validation profile, RMSD thresholds, and target count.

An xTB checkpoint continues one exact config hash and records every completed conformer atomically.
The xTB stage manifest is created with `status=running` before jobs start and refreshed
after every completed conformer. A caught interruption changes it to
`status=interrupted`; rerun the same configuration to reuse successful checkpoint entries
and finish the pending candidates.
