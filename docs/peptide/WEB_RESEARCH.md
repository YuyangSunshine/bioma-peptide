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

The supplied configuration uses the verified Volcengine endpoint (`backend: openai`, `model: deepseek-v4-1-flash-261017`, `api_base: https://ark.cn-beijing.volces.com/api/v3`, `api_key_env: HUOSHYAN_API_KEY`, `max_output_parameter: max_tokens`). The worker uses disabled thinking with 6,000 output tokens to avoid truncated tool-call JSON; its separate reviewer uses enabled thinking with 16,384 tokens. Gateway/proposal services also use disabled thinking. OpenRouter/Claude was exercised and a later short probe succeeded, but full research calls again returned HTTP 402 `in_flight_budget_exhausted`; it is not the active default. Historical configurations and provider failures are preserved locally. This is explicit configuration, not automatic fallback. Keep credentials outside the repository. It sets a 24-call model budget and a 32-call tool budget. When work is reviewable and no correction is pending, the final six calls or 120 seconds of the task deadline are reserved for submission, independent review and potential revision; this is a budget policy, not a scientific acceptance decision. `direct_tool_names` exposes the six registered target/material/job tools in the model tool list; it does not grant missing tools or prescribe an execution order. Other domain tools remain discoverable. The default `legacy` control policy is BioMA's continuous investigation harness, with independent delivery review; the name does not mean a fixed peptide pipeline. `--control-policy adaptive_v3` selects the separate existing scientific controller; this is not a claim that its complete live peptide path has been validated.

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

These test contract enforcement, bounded retrieval, source tampering, nested inspection, job idempotency, lifecycle receipts, production admission and external large-file references. Unit fixtures are not live model or scientific acceptance. The observed native Python/scientific package versions and configuration hash are recorded in [TESTED_ENVIRONMENT.json](TESTED_ENVIRONMENT.json); this is a measured environment manifest, not a complete lockfile. Live run records and the private import manifest remain local; see the current workspace's `.local-evidence/web-research` for actual observations.

A target name alone can start target research. It cannot supply missing peptide sequences, resolve stereochemical conflicts, approve parameter usage or define an unapproved production budget. Those prerequisites are reported as gaps rather than filled with invented scientific evidence.

## Recorded live validation (2026-10-31)

Actual Web runs, rather than mocked model replies, exercised the following paths:

- A target-only query (`人源 Nectin-4`) autonomously retrieved UniProt, selected PDB 4FRW, downloaded and independently inventoried its coordinates, revised its delivery and passed independent delivery review. Other listed structures were explicitly distinguished from the structure actually checked.
- A registered synthetic glycine topology job ran through the persistent broker. A subsequent Web session reattached to that same job, collected its files and independently recomputed graph/composition and file checks before passing delivery review. No second job was submitted. This is a small molecular-graph engineering test, not cyclic-peptide, binding or MD validation. The reviewer also flagged that 2D drawing distances cannot establish physical 3D bond-length validity; advisory review notes are shown alongside the report.
- Unapproved production submission was rejected for a missing registered production review. Local real-case approval conflicts and a candidate stereochemical discrepancy remain unresolved.

Successful delivery does not establish reliability for every query: earlier real runs failed on tool-budget exhaustion, report packaging, provider credits and transient source connections. Their failure records are retained. The current worker/reviewer configuration uses enabled reasoning; the target-only success above used the earlier disabled-reasoning configuration. An interrupted research action with an unresolved execution intent stops for reconciliation instead of being blindly replayed; durable scientific jobs remain independently addressable.

OpenRouter reported at least 5.486811 in captured completed-call cost fields before the provider-credit rejection. This is incomplete event accounting, not a billing reconciliation. Volcengine reported token counts but no monetary cost; its cost is unknown, not zero. Private snapshots, versions, exact run IDs and receipts are in `.local-evidence/web-research/REPORT.md`; they are excluded from publication.

A final target-only repeat after the UniProt field projection performed additional real sequence analysis and generated a target dossier, but stopped as `computed_pending_review`: independent review contained unconfirmed/ungrounded findings. Its computation and review checkpoint were retained, not rerun to force a pass. Thus the current model has demonstrated autonomous execution and some accepted reports, but reliable completion of complex reports and the full peptide research chain are not established.

An independent `uniprot-feature-location/1.0` checker now verifies one concrete source-interpretation failure: an unknown/fuzzy UniProt feature endpoint must not be asserted as an exact CSV endpoint. It uses BioPython's Swiss-Prot parser, requires a unique feature-type/annotation match, and abstains for unsupported or uncertainty-qualified schemas. A real generated table collapsed `32..?` to `32–32`; the checker independently confirmed that error. The delivery reviewer can request this bounded check and return verified repair feedback. This does not certify all region annotations or derived biological claims. Regression: 189 tests and 5 subtests passed, with three checker tests rerun after narrowing unsupported-schema handling.

The peptide configuration explicitly uses `review_history_mode: identity`: every historical file/receipt still has its actual hash verified, while unselected history is represented by identity, size and coverage notices. Complete selected support, source inputs, selected programs and outputs retain their existing review rules and budget; unselected history cannot support claims. The generic default remains `full` for baseline reproducibility. This prevents discarded code drafts from displacing selected evidence in the review context. The explicit 120-second review reserve complements call-count limits and does not approve results or extend task/job deadlines. Latest combined regression: 193 tests and 5 subtests passed, including history tampering and oversized selected-source rejection.

For a single complete Swiss-Prot source and a supported output CSV, the uncertain-endpoint check also runs automatically before model review. It can reject a collapsed unknown coordinate even if the model misses the issue. It does not infer source identity across multiple possible records. Final combined regression after this automatic gate: 194 tests and 5 subtests passed.

`investigate_review_gaps: true` returns hash-bound incomplete-review observations to the same Agent loop when budget remains. They are investigation leads, not facts or mandatory edits; the Agent may gather evidence or independently justify a revision. Unchanged submissions are not reviewed again, no approval is granted, and depleted budgets retain a durable pending checkpoint. Generic configuration defaults to the prior stop-on-incomplete behavior. Latest regression: 197 tests and 5 subtests passed.

## Accepted final autonomous loop

With the final provider split and review-gap continuation enabled, the literal query `人源 Nectin-4` completed a real Web run: UniProt retrieval → autonomous selection and download of 4JJH → independently recomputed structure inventory → first incomplete delivery assessment → Agent report revision → second independent assessment accepted. The HTTP response contains the hash-bound reviewed answer. No PDB ID or reference answer was supplied in the query. The run recorded nine completed model events and 188,780 total tokens; Volcengine did not report a monetary cost.

This establishes one actual execution/review/revision path, alongside the earlier persistent-job test. It does not establish universal query reliability, cyclic-peptide binding accuracy, or real Nectin-4 production-MD qualification. Earlier unsuccessful runs remain in local evidence.
