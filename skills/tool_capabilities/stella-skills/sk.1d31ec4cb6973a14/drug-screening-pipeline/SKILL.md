---
name: drug-screening-pipeline
description: 药物筛选管道技能：整合虚拟筛选、ADMET 性质预测、分子对接和配体-受体 相互作用分析，用于从化合物库中快速识别候选药物。
license: Apache-2.0
metadata:
  skill-author: STELLA Team
  domain: drug-discovery
  version: 1.0.0
  status: active
routing:
  keywords:
  - drug screening
  - virtual screening
  - molecular docking
  - ADMET
  - hit identification
  - lead compound
  - pharmacophore
  - binding affinity
  tags:
  - drug-discovery
  - virtual-screening
tools:
- predict_admet_properties
- run_molecular_docking
- analyze_protein_ligand_interactions
entry_conditions:
- 用户提供了蛋白质靶点（PDB ID 或序列）
- 用户提供了化合物库（SMILES 列表或数据库名称）
output_expectations:
- 排序的候选化合物列表（按对接得分）
- ADMET 性质过滤结果
- 关键结合残基分析
procedure:
- step_id: target_preparation
  title: 靶点蛋白准备
  goal: 准备对接就绪的蛋白质结构
  biological_rationale: 高质量的受体结构直接影响对接结果准确性
  instruction: |
    获取目标蛋白 PDB 结构，去除水分子和非关键配体。
    使用 predict_structure_esmfold（若无实验结构）。
    确认结合位点坐标（可使用 identify_binding_sites）。
  recommended_tools:
  - run_virtual_screening
  required: true
- step_id: ligand_preparation
  title: 配体库准备
  goal: 对化合物库进行初步 ADMET 过滤
  biological_rationale: 早期过滤低成药性化合物节省计算资源
  instruction: |
    使用 predict_admet_properties 过滤违反 Lipinski RO5 的化合物。
    同时过滤有毒基团（PAINS 过滤）。
  recommended_tools:
  - predict_admet_properties
  required: true
- step_id: virtual_screening
  title: 虚拟筛选
  goal: 快速筛选化合物库中与靶点结合的候选物
  biological_rationale: 高通量虚拟筛选将候选物从数万缩减至数百
  instruction: |
    使用 run_virtual_screening 进行基于形状/药效团的初步筛选。
    保留排名前 1% 的化合物进入精细对接阶段。
  recommended_tools:
  - run_virtual_screening
  required: true
- step_id: molecular_docking
  title: 分子对接
  goal: 精确预测候选分子与结合位点的结合模式和亲和力
  biological_rationale: 对接评分与实验 IC50 值具有中等相关性，用于优先化
  instruction: |
    使用 run_molecular_docking 对候选分子进行 Vina/Glide 对接。
    分析结合姿势，报告关键相互作用（氢键、疏水接触）。
  recommended_tools:
  - run_molecular_docking
  - analyze_protein_ligand_interactions
  required: true
---

# Drug Screening Pipeline

## Overview

Integrated virtual drug screening pipeline combining ADMET filtering, virtual screening,
and molecular docking. Designed to rapidly prioritize compounds from large libraries for
experimental validation.

## When to Use This Skill

- Hit identification from compound libraries against a protein target
- Repurposing existing drugs for new indications
- Lead optimization — comparing analogs by predicted binding affinity
- Fragment-based drug discovery

## Quick Start

```python
from bioagent.tools import run_molecular_docking, predict_admet_properties

# Filter compounds
admet_pass = predict_admet_properties(smiles_list, filters=["ro5", "pains"])
# Dock top candidates
docking_results = run_molecular_docking(target_pdb="1IEP", ligands=admet_pass[:100])
```
