---
name: small-molecule-analysis
description: 小分子化学信息学分析技能：基于 RDKit 计算分子性质（MW、LogP、TPSA、Lipinski 规则）， 进行分子相似性搜索，子结构匹配，以及 3D 构象生成与能量最小化。 适用于药物发现、虚拟筛选和分子性质预测。
license: Apache-2.0
metadata:
  skill-author: PredAgent Team
  domain: cheminformatics
  version: 1.0.0
  status: active
routing:
  keywords:
  - small molecule
  - drug discovery
  - molecular properties
  - SMILES
  - RDKit
  - Lipinski
  - molecular similarity
  - substructure search
  - 3D conformation
  - ADMET
  tags:
  - cheminformatics
  - drug-discovery
tools:
- compute_molecular_properties
- compute_molecular_similarity
- search_substructure
- generate_3d_conformation
- calculate_basic_mol_properties_with_rdkit
- find_similar_molecules_with_rdkit
- perform_substructure_search_with_rdkit
- generate_3d_conformation_with_rdkit
entry_conditions:
- 用户提供了分子的 SMILES 字符串或分子名称
output_expectations:
- 分子描述符（MW、LogP、HBD、HBA、TPSA 等）
- Lipinski Rule of Five 评估
- 相似分子列表（如请求）
- 3D 构象 PDB 文件（如请求）
procedure:
- step_id: compute_properties
  title: 计算分子性质
  goal: 计算药学相关的分子描述符
  biological_rationale: 分子性质直接影响药物口服生物利用度和成药性
  instruction: |
    使用 compute_molecular_properties 计算：
    - MW（分子量）：< 500 Da 为口服药物理想范围
    - LogP：-1 到 5 为理想范围（影响膜渗透性）
    - HBD（氢键供体）：≤ 5
    - HBA（氢键受体）：≤ 10
    - TPSA（极性表面积）：< 140 Å² 为口服药物理想值
    - Lipinski RO5 合规性
  recommended_tools:
  - compute_molecular_properties
  - calculate_basic_mol_properties_with_rdkit
  required: true
- step_id: similarity_search
  title: 分子相似性搜索
  goal: 在数据库中寻找与目标分子相似的已知活性化合物
  biological_rationale: 相似化合物往往具有相似活性（相似性原理）
  instruction: |
    使用 compute_molecular_similarity 计算 Morgan 指纹相似度（Tanimoto 系数）。
    相似度 > 0.7 认为高度相似；< 0.3 认为不相关。
  recommended_tools:
  - compute_molecular_similarity
  - find_similar_molecules_with_rdkit
  required: false
- step_id: substructure_search
  title: 子结构搜索
  goal: 检测分子中特定官能团或骨架的存在
  biological_rationale: 特定药效团和官能团决定分子活性机制
  instruction: |
    使用 search_substructure 搜索目标 SMARTS 模式。
    常用药效团：苯环、羟基、羧基、胺基等。
  recommended_tools:
  - search_substructure
  - perform_substructure_search_with_rdkit
  required: false
- step_id: conformer_generation
  title: 3D 构象生成
  goal: 生成分子的低能量 3D 构象用于对接或可视化
  biological_rationale: 3D 构象是分子对接和形状相似性计算的必要输入
  instruction: |
    使用 generate_3d_conformation 生成最低能量构象。
    使用 MMFF94 力场进行几何优化。
  recommended_tools:
  - generate_3d_conformation
  - generate_3d_conformation_with_rdkit
  required: false
---

# Small Molecule Analysis

## Overview

This skill provides comprehensive cheminformatics analysis of small molecules using RDKit.
It covers drug-likeness assessment (Lipinski, Veber rules), molecular fingerprint similarity,
substructure matching, and 3D conformation generation.

## When to Use This Skill

- Assessing drug-likeness of candidate compounds
- Comparing molecular similarity to known drugs
- Identifying chemical scaffolds and functional groups
- Generating 3D structures for docking
- Filtering compound libraries by physicochemical properties

## Quick Start

```python
from bioagent.tools import compute_molecular_properties

# Aspirin
props = compute_molecular_properties("CC(=O)Oc1ccccc1C(=O)O")
# Returns: {"MW": 180.16, "LogP": 1.43, "HBD": 1, "HBA": 3,
#           "TPSA": 63.6, "Lipinski": True}
```

## Lipinski Rule of Five

| Property | Threshold | Impact |
|----------|-----------|--------|
| MW | ≤ 500 Da | Absorption |
| LogP | ≤ 5 | Membrane permeability |
| HBD | ≤ 5 | Permeability |
| HBA | ≤ 10 | Solubility |

## Common Pitfalls

- SMILES must be sanitized before RDKit processing
- Tanimoto similarity depends on fingerprint type (Morgan vs. MACCS keys)
- 3D generation may fail for highly strained rings — check RMSD after minimization
