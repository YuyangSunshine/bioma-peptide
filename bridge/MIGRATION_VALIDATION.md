# Domain increment migration and acceptance

The feature branch contains the existing standalone bridge plus later domain increments:
ensemble contracts, parameter-source tooling, complex relaxation, receptor preparation,
MD system build, minimization, equilibration and restartable production implementation.
The two peptide skills update their existing catalog paths; no duplicate runtime skill
IDs are added. `migration-provenance.json` records actual source and migrated hashes.
No private BioMA controller/runtime, parameter bundle, case sequence, experimental data
or scientific output is included in this public change.

Portability changes remove the embedded case sequence and machine-specific paths.
The parameter-source CLI requires `--sequence`, and the tool adapter requires an explicit
`parameter_source.sequence`. Docking command templates are parsed into argv with
`shell=False`; shell pipelines must be replaced with a reviewed fixed executable wrapper.
The historical receptor-preparation module remains a case-scoped implementation, not
an arbitrary-target modelling service. Likewise the docking skill's +5/TBMB assumptions
must be restricted by the reviewed task profile. These modules do not themselves
provide a new production-use approval.

## Executed acceptance

- 34 targeted tests passed using Python 3.11 in the actual scientific environment,
  including all ten tool schemas, manifest and input identity, failure propagation,
  submission idempotency, real process cancellation/resume, production admission,
  migrated MD-stage regressions, and literal external arguments without shell expansion.
- A real persistent broker job executed the migrated RDKit glycine topology tool;
  execution, artifact validation and independent graph validation all passed.
- A bounded two-particle OpenMM CUDA probe started the real engine, restored a checkpoint
  with an energy tolerance of 1e-5 kJ/mol, and terminated its controlled child process.
  This direct probe is not a peptide simulation or persistent GPU broker qualification.
- Existing case evidence was copied to a Git-ignored local snapshot with original hashes.
  Independent validation still found a stereochemical mismatch in one of three candidates.
  Non-isomeric graph agreement does not establish stereochemical identity.

The full case pipeline has **not passed**. Conflicting parameter/receptor review records
remain unresolved, and no independent scientific approval was fabricated. Production MD
is disabled. No fresh docking-to-production chain was run and no new production trajectory
was generated. Passing engineering tests is not authorization to merge under a requirement
that the full scientific workflow pass first.

## Reproduce targeted tests

From the repository root, using a scientific Python with RDKit, PyYAML, pydantic and pytest:

```bash
export PYTHONPATH="$PWD/bridge/src"
export PEPTIDE_SOURCE_ROOT="$PWD"
python -m pytest bridge/tests -q -p no:cacheprovider
python -m bioma_peptide_bridge.cli catalog --source-root "$PWD"
```

The process lifecycle tests require a host that permits detached controlled subprocesses.
A restricted sandbox run had 31 passes, one skip and one cancellation failure; the host
rerun and final complete suite passed. The OpenHands environment does not include pytest;
no dependency was silently installed into it.

Local case configurations and evidence remain under `.local-evidence/`, excluded from Git.
Do not force-add that directory. Large trajectories remain external, hash-bound references.
