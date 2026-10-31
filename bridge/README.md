# Peptide bridge

Runtime-independent contracts and a local persistent worker for ten existing
peptide tools. This distribution provides only `bioma_peptide_bridge`; it does
not install `bioma`, OpenHands, models or scientific engines. Do not install two
BioMA distributions in one environment.

Python >=3.11, Linux `/proc`, POSIX file locks and local SQLite storage are required.
Core contracts/worker use the standard library. Scientific children additionally
need the dependencies of their selected tool (Pydantic, PyYAML, RDKit, etc.).
Use the scientific Python to submit scientific jobs, or bind an operator-selected
`interpreter` path and SHA256 in the profile; the supervisor verifies it before
starting the child. Controller and scientific dependencies can remain separate. Installing the bridge alone does not install those dependencies.

```bash
python -m pip install --no-deps ./bridge
bioma-peptide catalog --source-root /path/to/single/source-checkout
bioma-peptide submit --root /controlled/jobs --registry /controlled/registry.json --request request.json
bioma-peptide status --root /controlled/jobs --registry /controlled/registry.json --job-id job-...
```

`collect`, `cancel`, `reattach` and `resume` have the same job-ID interface.
A cancellation request is distinct from the worker's final cancellation receipt.
The supervisor kills the whole controlled process group, including residual
children. Session exit does not terminate the detached supervisor. Reattachment
never submits another job. SQLite uniqueness and an OS lock bind an idempotency
key to the complete registered request; altered parameters are rejected.

The trusted registry specifies `input_root`, hash-pinned `inputs`, and `profiles`.
Each profile includes `method`, `approved`, `review_id`, `inputs`, `identity`,
`resource_caps` and (for domain calls) source root/hashes and structured arguments.
Agent submissions contain only profile/input IDs, idempotency key and five
positive integer budgets: `seconds`, `cpu_seconds`, `memory_bytes`, `output_bytes`,
`cpus` (plus `gpu_memory_mib` for a GPU profile). Keep the registry, job root and source tree outside the Agent's mounts.
The registry is an operator-owned authorization record, not an Agent-editable
file or a cryptographic attestation service.

Arguments use `$input:ID` for registered files, `$json:ID` for registered JSON
content and `$output` for the job output directory. Transitive JSON/YAML input
references must be registered too. Absolute unregistered paths and traversal
are rejected. Only pinned programs can run, with `shell=False`; shell templates
with pipelines/redirections must be replaced by structured, single-program
invocations. Domain subprocesses may not start new sessions.

All ten tool definitions are loaded from pinned existing source in an isolated
child. Their schema/version is retained, with explicit `computed_prediction`
classification and a fail-closed result validator. This does not change the old
runtime's registry or remove its authorization filter. Only reviewed broker
profiles grant invocation. Existing docking still has a +5 / selected-polymer
scope: a profile must explicitly match it, with target/domain/retained chains.
Other chemistry or receptor retention policies are blocked until that specific
method is extended and independently qualified.

`execution_status`, `artifact_validation_status` and
`scientific_validation_status` are separate. Raw JSON string stage failures,
unknown statuses and interruptions never become scientific success. Partial
success exposes only independently validated candidate IDs. Stage manifest
identity is checked against the result, and files against a supervisor-frozen
hash manifest. The graph validator is explicitly limited to canonical chemical
graph, charge, stereochemistry and connectedness, with pinned RDKit version.
It does not qualify conformations, docking, receptor preparation or MD.
Other stages remain scientifically `not_run` until a fixed appropriate checker
is implemented; an Agent's success claim cannot supply it.

All outputs use external URI/size/SHA256/version/producer/identity/retention
references, including files larger than 32 MiB. Hashes are streamed in 1 MiB
chunks. Changed/missing files are rejected on collection. Content-hash versions
are logical immutable identities, not a WORM storage guarantee; retain the files
until review and treat any later mutation as an identity failure. No file bodies
or base64 trajectories enter the tool observation.

The supervisor enforces wall time and output budget; sets per-process CPU,
address-space, file-size and descriptor limits; sets CPU affinity; and samples
process-group CPU/RSS/process count every ~100 ms. Sampling is not a hard cgroup
aggregate memory cap. Only the fixed trusted programs are supported, not hostile
multi-tenant workloads. No real GPU profile has been qualified by this migration. The local GPU path
accepts one registered GPU UUID, a hash-pinned `nvidia-smi`, an explicit virtual
address-space allowance (separate from sampled resident memory), and device-memory
budget. It uses a cooperative project lease and samples process-specific GPU
memory; this is not a hardware VRAM partition or a cross-project scheduler.

The `tbmb_md_production` method binds the migrated OpenMM stage implementation and its
hash-checked segment checkpoints. It accepts only registered Stage-4 input bundles
and a fixed profile configuration. `staged_path` preserves the registered bundle
layout, and `$bundle:relative-directory` refers to that layout. No production
argument, shell command, arbitrary engine path, or approval flag comes from the
Agent. `md_canary` requires a registered independent review binding inputs,
chemistry/receptor approval, engine/interpreter, exact candidate scope, arguments,
GPU identity and resource caps. `production_md` additionally requires an external
qualification record for engine start, output checks, cancellation, resume and
budget enforcement with that same binding. These records are operator-owned
JSON inputs; the bridge never generates or fills in scientific approval. The
current real Nectin-4 profile remains blocked pending reconciliation of its
existing approval records and a migration canary. No approximate parameters or
new production trajectory are generated by this migration.

Resume retains the same job ID/output directory and verifies the complete
registry, inputs and profile. xTB retains its candidate checkpoints; the MD adapter retains the existing
engine's hash-validated segments. The synthetic canary may restart its fixed workload. A live orphan after
supervisor loss requires operator reconciliation; it is not automatically
restarted or relabelled as cancelled. Other engines need their own checkpoint
adapter. Do not describe the CPU engineering canary as GPU/MD qualification.

`data.measurement` keeps source/version, candidate/target/chemical identities,
measurement kind, units/conditions/construct/replicates and raw missing markers.
`/` and `-` remain unspecified missing values. Experimental Kd, screening,
docking and MD metrics are separate kinds. `check_split` rejects family, alias
or replicate leakage across dataset splits.

```bash
PYTHONPATH=bridge/src python -m unittest discover -s bridge/tests -v
# Also load the actual ten schemas with a scientific Python:
PEPTIDE_SOURCE_ROOT=/path/to/source PYTHONPATH=bridge/src python -m unittest discover -s bridge/tests -v
```

Rollback: stop using this CLI/adapter and invoke the original entry in its original
environment. Do not reset a research branch or delete persistent jobs. Existing
workers continue within their own budgets; query/cancel them by job ID first if
needed. No default branch or old runtime entry is modified by installing this package.
