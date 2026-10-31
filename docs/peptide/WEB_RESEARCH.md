# Autonomous research from the Web

The Web submits a question to the existing BioMA research loop. It does not execute a fixed sequence of peptide stages. The model discovers tools, selects targets and structures, inspects evidence, submits work for independent review and responds to review feedback. `src/bioma/peptide` contains domain contracts, controlled jobs and source validators; it is not a second Agent loop.

## Start from the repository

```bash
python -m pip install -e '.[peptide,analysis,dev]'
bioma-peptide-web --config configs/peptide_web.yaml \
  --env-file /path/to/private/runtime.env \
  --host 127.0.0.1 --port 8015 \
  --query-deadline-seconds 600 --task-deadline-seconds 480
```

The supplied configuration uses the verified Volcengine-compatible endpoint with `backend: openai`, `api_base`, `api_key_env: HUOSHYAN_API_KEY`, `model: deepseek-v4-1-flash-261017`, `max_output_parameter: max_tokens` and explicit thinking settings. The research worker and independent reviewer use `thinking_mode: enabled` with 16,384 output tokens; gateway/proposal calls use disabled thinking with 6,000 tokens. Forced tool choice with thinking enabled was verified against the live endpoint. OpenRouter was also exercised, but returned an in-flight-credit 402 during acceptance; its configuration snapshots remain in local evidence. Provider/model selection is explicit, never a silent fallback. Keep credentials outside the repository. It sets a 24-call model budget and a 32-call tool budget. When work is reviewable, the final six calls are reserved for submission, independent review and potential revision; this is a budget policy, not a scientific acceptance decision. `direct_tool_names` exposes the six registered target/material/job tools in the model tool list; it does not grant missing tools or prescribe an execution order. Other domain tools remain discoverable. The default `legacy` control policy is BioMA's continuous investigation harness, with independent delivery review; the name does not mean a fixed peptide pipeline. `--control-policy adaptive_v3` selects the separate existing scientific controller; this is not a claim that its complete live peptide path has been validated.

Optional host-maintained long-job registry:

```bash
bioma-peptide-web --config configs/peptide_web.yaml \
  --env-file /path/to/private/runtime.env --host 127.0.0.1 --port 8015 \
  --peptide-registry /path/to/private/registry.json \
  --peptide-job-root /path/to/persistent/jobs
```

Use a scientific Python environment containing the registered engines. No arbitrary host command, path or approval is accepted from the model. A registry profile binds the method, inputs, identity, validator and resource caps. Missing registry configuration blocks job submission, without preventing public target research. An unapproved production profile remains blocked.

## Available actions

| Action | Authority and limits |
|---|---|
| `query_uniprot` | Searches live UniProt, returns multiple organisms and PDB cross-references; no automatic first-hit selection. The source request explicitly selects identity, organism, sequence and PDB fields; the complete returned JSON is retained and hash-bound. The validator recomputes the result from that source. This discovery projection is not the full functional annotation. |
| `assess_target_structure` | Downloads selected PDB mmCIF from RCSB; independently recomputes entry identity, chains, coordinates, residue and heteroatom counts. Does not certify receptor preparation, binding, force fields or MD suitability. |
| `list_project_materials`, `read_project_material` | Search imported local originals and verify each selected file against the import manifest. Large files remain external references. Original unresolved approvals stay unresolved. |
| `list_peptide_profiles`, `manage_peptide_job` | Discover registered computations; submit/status/collect/cancel/resume/reattach durable jobs. Counts against the existing research tool budget. A successful lifecycle action is not a successful scientific computation. Verified small structure/JSON outputs are admitted for further analysis; large trajectories remain external references. |
| Existing inspection, analysis, skills and delivery tools | Preserve autonomous selection, executable analysis, bounded observation, independent review and revision. |

The ten raw crosslinked/TBMB tools remain registered. Direct calls lacking trusted scientific context are rejected before expensive execution and point to registered jobs. This avoids silently treating a JSON `failed_topology`, unknown state or partial result as success. New methods still need an appropriate independent scientific validator; a graph checker cannot certify docking or MD.

## Local material and provenance

The current workspace imports necessary original material under `.local-data/nectin4`, excluded from Git. `manifest.json` records original location, relative local location, bytes and SHA256. The original source is retained for provenance; copying a file does not grant scientific approval. The import includes candidate structures, receptor evidence, parameter packages and existing preparation-stage artifacts. Production trajectories are not copied into Agent context.

The later domain implementation and two core peptide skills were already migrated into their native repository locations. See [migration-provenance.json](migration-provenance.json); their source hashes and subsequent adaptation hashes are distinct. Do not copy a second private BioMA runtime over the public project.

## Regression checks

```bash
python -m pytest tests/peptide tests/test_web.py tests/test_shared_capability_negative_gates.py -q
```

These test contract enforcement, bounded retrieval, source tampering, nested inspection, job idempotency, lifecycle receipts, production admission and external large-file references. Unit fixtures are not live model or scientific acceptance. Live run records and the private import manifest remain local; see the current workspace's `.local-evidence/web-research` for actual observations.

A target name alone can start target research. It cannot supply missing peptide sequences, resolve stereochemical conflicts, approve parameter usage or define an unapproved production budget. Those prerequisites are reported as gaps rather than filled with invented scientific evidence.

## Recorded live validation (2026-10-31)

Actual Web runs, rather than mocked model replies, exercised the following paths:

- A target-only query (`人源 Nectin-4`) autonomously retrieved UniProt, selected PDB 4FRW, downloaded and independently inventoried its coordinates, revised its delivery and passed independent delivery review. Other listed structures were explicitly distinguished from the structure actually checked.
- A registered synthetic glycine topology job ran through the persistent broker. A subsequent Web session reattached to that same job, collected its files and independently recomputed graph/composition and file checks before passing delivery review. No second job was submitted. This is a small molecular-graph engineering test, not cyclic-peptide, binding or MD validation. The reviewer also flagged that 2D drawing distances cannot establish physical 3D bond-length validity; advisory review notes are shown alongside the report.
- Unapproved production submission was rejected for a missing registered production review. Local real-case approval conflicts and a candidate stereochemical discrepancy remain unresolved.

Successful delivery does not establish reliability for every query: earlier real runs failed on tool-budget exhaustion, report packaging, provider credits and transient source connections. Their failure records are retained. The current worker/reviewer configuration uses enabled reasoning; the target-only success above used the earlier disabled-reasoning configuration. An interrupted research action with an unresolved execution intent stops for reconciliation instead of being blindly replayed; durable scientific jobs remain independently addressable.

OpenRouter reported at least 5.486811 in captured completed-call cost fields before the provider-credit rejection. This is incomplete event accounting, not a billing reconciliation. Volcengine reported token counts but no monetary cost; its cost is unknown, not zero. Private snapshots, versions, exact run IDs and receipts are in `.local-evidence/web-research/REPORT.md`; they are excluded from publication.

A final target-only repeat after the UniProt field projection performed additional real sequence analysis and generated a target dossier, but stopped as `computed_pending_review`: independent review contained unconfirmed/ungrounded findings. Its computation and review checkpoint were retained, not rerun to force a pass. Thus the current model has demonstrated autonomous execution and some accepted reports, but reliable completion of complex reports and the full peptide research chain are not established.
