---
name: binding-pocket-detection
description: |
  Detect and rank binding pockets on apo or predicted protein structures with fpocket or
  P2Rank, returning pocket centers, lining residues, and ligandability scores. Use when
  no bound ligand already defines the site or when an ensemble must be screened for
  transient pockets.
license: MIT
metadata:
  skill-author: BioMA
  domain: structural-biology
  status: active
  source: https://github.com/GPTomics/bioSkills
routing:
  keywords:
    - binding pocket detection
    - fpocket
    - P2Rank
    - apo pocket
    - cryptic pocket
  tags:
    - pocket
    - binding-site
    - structural-biology
tools:
  - detect_binding_pockets
  - identify_binding_sites

entry_conditions:
  - "A prepared local PDB structure is available."

output_expectations:
  - "Ranked pocket centers, residues, scores, method, and source structure."
  - "Pocket ranks are reported as hypotheses, not proof of biological function."

procedure:
  - step_id: choose_detection_mode
    title: Choose holo mapping or de novo detection
    instruction: |
      If a relevant ligand is already present, use identify_binding_sites to map its
      observed interface. For an apo structure, use detect_binding_pockets with fpocket
      or P2Rank. Do not call a geometric cavity an experimentally validated site.
    recommended_tools: [identify_binding_sites, detect_binding_pockets]
    required: true

  - step_id: corroborate
    title: Corroborate the pocket
    instruction: |
      Prefer pockets supported by both geometry and biological evidence. A predicted
      structure may have unreliable pocket-lining side chains even when backbone pLDDT
      is high. A cryptic site requires multiple conformations rather than one snapshot.
    required: true
---

# Binding Pocket Detection

This is a concise BioMA adaptation of the MIT-licensed GPTomics `binding-site-detection`
skill. It intentionally keeps only the fpocket/P2Rank workflow used by this project.
