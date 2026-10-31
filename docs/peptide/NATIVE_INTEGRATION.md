# Native peptide integration

The migration-only `bridge/` distribution has been removed. Execution contracts,
persistent jobs, data semantics and scientific checks now live in `bioma.peptide`.
There is one root distribution and one `bioma-peptide` CLI. The ten domain tools
now declare their result classification and validator directly in their decorators;
the loader no longer replaces Tool dataclasses or synthesizes `bioma` modules.

`bioma` uses lazy public exports: `from bioma import BioMA` still resolves to the
same class, while importing the job service does not initialize the research API.
The existing scientific-control and harness decision code is retained. This change
does not claim every MD stage is exposed to the Agent or that scientific production
acceptance is complete.

Validation on the native revision:

- 35 peptide tests plus 31 evidence-driven control/revision tests passed; 5 subtests passed.
- Real migrated RDKit glycine topology job executed under `bioma.peptide.worker`,
  with artifact and independent graph validation passing.
- Root wheel built offline and installed without dependencies into a clean temporary
  environment; lightweight CLI catalog and the README persistent-job example passed.
  This tests lightweight services, not the full research API without dependencies.
- The wheel contains the native namespace and no former bridge namespace or local evidence.
- README relative links and `git diff --check` passed.

The first relocation run exposed a worker PYTHONPATH offset error (four lifecycle
failures). The native source-root calculation was corrected; lifecycle tests and
real job execution were rerun successfully. Failures were not reclassified as passes.

Breaking installation changes: Python >=3.11; imports move from
`bioma_peptide_bridge` to `bioma.peptide`; install the root project, not `./bridge`.
No compatibility alias is retained. Do not rewrite existing job source pins to
bypass checkpoint identity checks. Old live workers require their original environment
until completion or confirmed cancellation.

No new model call or production MD was run for this refactor. Historical private
OpenHands validation and synthetic GPU probes retain their original limited scope.
