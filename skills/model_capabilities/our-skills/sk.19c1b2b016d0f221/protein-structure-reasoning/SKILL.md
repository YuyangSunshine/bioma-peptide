---
name: protein-structure-reasoning
description: 蛋白质三维结构分析技能：从 PDB 获取实验结构，解析多链复合物，识别结合位点， 使用 ESMFold/OmegaFold 预测结构，并通过 AMBER 力场进行结构松弛优化。 适用于结构生物学分析、药物靶点识别和蛋白质-配体互作研究。
license: Apache-2.0
metadata:
  skill-author: PredAgent Team
  domain: structural-biology
  version: 1.0.0
  status: active
routing:
  keywords:
  - protein structure
  - PDB
  - binding site
  - structural prediction
  - ESMFold
  - OmegaFold
  - AMBER
  - 3D structure
  - active site
  tags:
  - structural-biology
  - drug-discovery
tools:
- fetch_pdb_structure
- fetch_pdb_by_id
- parse_pdb_chains
- identify_binding_sites
- get_sequence_from_pdb_with_biopython
- find_binding_site_residues_with_biopython
- predict_structure_esmfold
- esmf_predict_protein_structure
- omegafold_predict_structure
- mmseqs2_generate_msa
- relax_structure_amber
- amber_relax_structure
- compute_structure_energy
- evoef2_calculate_energy
- saprot_predict_function
entry_conditions:
- 用户提供了 PDB ID 或蛋白质序列（用于结构预测）
output_expectations:
- PDB 结构文件或解析后的链信息
- 结合位点残基列表
- 结构预测置信度分数（pLDDT）
procedure:
- step_id: fetch_structure
  title: 获取实验结构
  goal: 从 RCSB PDB 数据库获取蛋白质三维结构
  biological_rationale: 实验解析的结构提供最可靠的原子坐标信息
  instruction: |
    使用 fetch_pdb_structure 获取 PDB 文件。若无实验结构，
    使用 predict_structure_esmfold、esmf_predict_protein_structure 或
    omegafold_predict_structure 预测结构。需要原始 PDB 内容时可用 fetch_pdb_by_id。
  recommended_tools:
  - fetch_pdb_structure
  - fetch_pdb_by_id
  required: false
- step_id: parse_chains
  title: 解析结构链
  goal: 提取感兴趣的链和残基信息
  biological_rationale: 大多数 PDB 条目包含多链复合物，需要识别功能相关链
  instruction: |
    使用 parse_pdb_chains 解析所有链的序列和长度。
    识别配体（HETATM）和辅因子。
  recommended_tools:
  - parse_pdb_chains
  - get_sequence_from_pdb_with_biopython
  required: true
- step_id: identify_binding_sites
  title: 识别结合位点
  goal: 预测功能性结合口袋和活性位点
  biological_rationale: 结合位点识别是药物设计和功能注释的关键步骤
  instruction: |
    使用 identify_binding_sites 基于几何和进化保守性预测结合口袋。
    输出每个口袋的中心坐标、体积和关键残基。
  recommended_tools:
  - identify_binding_sites
  - find_binding_site_residues_with_biopython
  required: false
- step_id: structure_prediction
  title: 从头结构预测
  goal: 对无实验结构的蛋白质进行 AI 结构预测
  biological_rationale: ESMFold/OmegaFold 可在数秒内预测高质量结构
  instruction: |
    使用 predict_structure_esmfold 进行快速本地/公开 API 预测；若任务需要服务器
    API 版结构文件 URL，使用 esmf_predict_protein_structure 或 omegafold_predict_structure。
    pLDDT > 70 的区域可信度高，< 50 区域为无序区域。
  recommended_tools:
  - predict_structure_esmfold
  - esmf_predict_protein_structure
  - omegafold_predict_structure
  - mmseqs2_generate_msa
  required: false
- step_id: structure_relaxation
  title: AMBER 力场松弛
  goal: 消除结构预测的原子碰撞，优化几何构象
  biological_rationale: 力场优化改善结构质量，适合后续分子对接使用
  instruction: |
    有 PDB 文本时使用 relax_structure_amber；有结构 URL 时使用 amber_relax_structure。
    需要结构能量或稳定性时可使用 compute_structure_energy、evoef2_calculate_energy
    或 saprot_predict_function。
  recommended_tools:
  - relax_structure_amber
  - amber_relax_structure
  - compute_structure_energy
  - evoef2_calculate_energy
  - saprot_predict_function
  required: false
---

# Protein Structure Reasoning

## Overview

This skill enables comprehensive 3D protein structure analysis, from fetching experimental
structures to predicting novel ones and identifying functional sites. It integrates PDB
retrieval, AI-based structure prediction (ESMFold, OmegaFold), and molecular mechanics
energy minimization.

## When to Use This Skill

- Analyzing experimentally resolved protein structures from PDB
- Predicting structures of proteins without experimental data
- Identifying drug binding sites and active sites
- Preparing structures for molecular docking studies
- Comparing wild-type vs. mutant structural changes

## Quick Start

```python
from bioagent.tools import fetch_pdb_structure, identify_binding_sites

# Fetch EGFR kinase structure
structure = fetch_pdb_structure("1IEP")
sites = identify_binding_sites(structure, method="fpocket")
```

## Structure Quality Assessment

| pLDDT Score | Confidence |
|-------------|------------|
| > 90 | Very high — backbone and side-chains accurate |
| 70–90 | High — backbone accurate, side-chains may vary |
| 50–70 | Low — topology likely correct, details unreliable |
| < 50 | Very low — likely intrinsically disordered |

## Common Pitfalls

- Always check resolution of experimental structures (< 2.5Å preferred for drug design)
- Remove water molecules and ligands before ESMFold input
- AMBER relaxation may take several minutes for large proteins (> 500 aa)
