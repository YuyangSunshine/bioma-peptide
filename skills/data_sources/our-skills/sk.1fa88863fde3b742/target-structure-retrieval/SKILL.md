---
name: target-structure-retrieval
description: |
  Retrieve experimental RCSB PDB structures and AlphaFold DB predicted structures for a
  protein target. Use for Target IDs or UniProt accessions when downstream pocket,
  complex, or energy analysis needs PDB/mmCIF coordinates and prediction confidence.
license: Apache-2.0
metadata:
  skill-author: BioMA
  domain: structural-biology
  status: active
  source: https://github.com/google-deepmind/science-skills
routing:
  keywords:
    - target structure retrieval
    - AlphaFold DB
    - AFDB
    - RCSB PDB
    - mmCIF
  tags:
    - target
    - protein-structure
    - structure-retrieval
tools:
  - fetch_pdb_by_id
  - fetch_alphafold_structures
  - parse_pdb_chains

entry_conditions:
  - "A PDB ID or UniProt accession is available."

output_expectations:
  - "Coordinate URLs or downloaded PDB/mmCIF paths, with source and identifier."
  - "All AFDB fragments returned for the accession; do not silently keep only one."
  - "Predicted structures are labeled as predictions, not experimental evidence."

procedure:
  - step_id: retrieve_experimental
    title: Retrieve experimental structures
    instruction: |
      For a four-character PDB ID, call fetch_pdb_by_id. Preserve biological chain
      identity and report that the coordinates came from RCSB PDB.
    recommended_tools: [fetch_pdb_by_id]
    required: false

  - step_id: retrieve_prediction
    title: Retrieve AlphaFold DB structures
    instruction: |
      For a UniProt accession, call fetch_alphafold_structures. Prefer mmCIF and provide
      output_dir when downstream tools require local files. Keep every fragment returned
      by AFDB and retain PAE links for confidence-aware interpretation.
    recommended_tools: [fetch_alphafold_structures]
    required: false

  - step_id: report_provenance
    title: Report provenance and limitations
    instruction: |
      Distinguish experimental and predicted coordinates. An AFDB model is generally one
      predicted state, not a thermodynamic conformational ensemble.
    required: true
---

# Target Structure Retrieval

This skill adapts the AlphaFold Database and PDB retrieval workflows published in Google
DeepMind's Apache-2.0 `science-skills` repository to BioMA's registered-tool interface.
