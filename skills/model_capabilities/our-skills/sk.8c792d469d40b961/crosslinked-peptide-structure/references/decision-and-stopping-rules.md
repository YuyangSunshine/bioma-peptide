# Decision and stopping rules

Let `K` be the requested final structure count and `L` the peptide length.

## Initial sizes

```text
RDKit raw = round_up_25(max(100, 20K, 10L))
normal target = round_up_8(max(3K, 16))
normal ceiling = round_up_8(max(6K, 64))
tight target = round_up_8(max(3K, 16))
```

For `K=10` and `L=16`, use 200 RDKit structures, 32 normal candidates, a 64-candidate expansion ceiling, 32 tight candidates, and 10 final representatives.

Pass `K` itself to `generate_rdkit_conformer_ensemble`. Do not multiply `K` before the call because the tool already applies the `20K` and `10L` raw-sampling rules.

The RDKit total is distributed across random seeds. Eight seeds for 200 structures generate about 25 structures per seed.

## Selection

The first Tool 4 call validates the RDKit candidates, applies optional MMFF94s collision relief with complete parameter coverage, performs 1.0 Å core-heavy-atom RMSD de-duplication, and uses farthest-point diversity selection for the normal-xTB starting set. Topology and geometry validation remain mandatory for every selected structure.

The second Tool 4 call validates normal xTB results, clusters them at 1.5 Å core-heavy-atom RMSD, takes half of the tight set by energy rank, and fills the other half by farthest-point diversity among remaining cluster representatives.

The third Tool 4 call validates tight xTB results, clusters them at 1.5 Å, assigns the minimum-energy member as each cluster representative, and selects the lowest-energy representatives from distinct clusters.

xTB energy participates in ranking without a fixed energy-window cutoff. Relative energies compare structures with the same composition, charge, method, and solvent model.

## Expansion

Add candidates in bounded batches when a stage returns `insufficient_candidates`. Stop expansion at the calculated ceiling and report the validated cluster count. Each selected candidate ID remains unique throughout the workflow.

Every additional RDKit batch uses a different `base_seed`. A convenient deterministic sequence is `next_base_seed = previous_base_seed + seed_count * 104729`. Merge the original and additional raw manifests in the next `select_for_normal` call. A repeated configuration hash or repeated result hash does not count as additional sampling.

When expanding normal candidates, pass the preceding `select_for_normal` manifest as
`prior_selection_manifest_path`. The selection tool carries those candidate IDs forward,
excludes them from the new selection, and emits only the additional candidates required
to reach the normal ceiling. Optimize only the emitted additional candidates. Merge the
original and additional normal-xTB manifests in the next `select_for_tight` call so
previously converged and validated coordinates are reused rather than recomputed or lost.

When every candidate fails only one identical validation check, the selection stage returns `validation_contract_error` with `recommended_action=inspect_validation_contract`. Stop sampling at that point; inspect or correct the failed validation definition before continuing.
