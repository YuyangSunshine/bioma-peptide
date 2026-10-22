---
name: protein-developability-screening
description: 蛋白质可开发性筛查技能：调用真实可用的 Tm、耐高温、pH、溶解度、 结构感知稳定性和结构能量工具，综合评估蛋白质候选的工程可行性。
license: Apache-2.0
metadata:
  skill-author: PredAgent Team
  domain: protein-engineering
  version: 2.0.0
  status: active
routing:
  keywords:
  - protein stability
  - thermal stability
  - melting temperature
  - Tm
  - solubility
  - aggregation
  - optimal pH
  - developability
  - manufacturability
  tags:
  - protein-engineering
  - biopharmaceuticals
  - developability
tools:
- evaluate_mutation_matrix_with_developability_tools
- fold_mutation_matrix_structures
- score_folded_mutation_matrix_structures
- filter_r7_candidate_matrix
- write_r7_top5_auto_artifacts
- write_r7_top5_artifacts
- pptstab_predict_tm
- deeptp_predict_protein_properties
- ephod_predict_optimal_ph
- netsolp_predict_solubility
- saprot_predict_function
- evoef2_calculate_energy
- esmf_predict_protein_structure
- omegafold_predict_structure
- compute_structure_energy
entry_conditions:
- 用户提供了蛋白质序列、候选突变序列、UniProt ID 或结构 URL
- 需要评估热稳定性、pH、溶解度、可生产性、结构稳定性或结构能量
output_expectations:
- 明确区分真实工具输出、模型预测和启发式解释
- Tm 使用 PPTStab；耐高温分类使用 DeepTP；溶解度使用 NetSolP；pH 使用 EpHod
- 结构漏斗使用本地 PDB pLDDT；有结构 URL 时补充 EvoEF2 能量
- 不得把 ESM LLR 或手写规则伪装成真实 Tm、溶解度或结构能量
procedure:
- step_id: sequence_developability
  title: 序列级可开发性预测
  goal: 从完整蛋白序列预测 Tm、耐高温倾向、最适 pH 和溶解度
  biological_rationale: 序列级模型可快速覆盖大量候选，用于首轮筛选
  instruction: |
    对包含 candidate_id 和 full_sequence 的候选矩阵，优先使用
    evaluate_mutation_matrix_with_developability_tools 批量评估，避免逐条手工调用造成
    上下文膨胀和转录错误。
    评估完成后，如任务要求从候选矩阵中继续缩小范围，使用
    filter_r7_candidate_matrix 读取 CSV 并写出 shortlist；不要让 LLM 手工复制或裁剪 CSV。
    对完整蛋白序列调用 pptstab_predict_tm、deeptp_predict_protein_properties、
    ephod_predict_optimal_ph 和 netsolp_predict_solubility。记录工具名、输入序列 ID、
    数值字段和失败状态。不要在工具失败时编造替代数值。
  recommended_tools:
  - evaluate_mutation_matrix_with_developability_tools
  - filter_r7_candidate_matrix
  - pptstab_predict_tm
  - deeptp_predict_protein_properties
  - ephod_predict_optimal_ph
  - netsolp_predict_solubility
  required: true
- step_id: structure_prediction
  title: 结构预测
  goal: 在缺少结构 URL 时生成候选结构
  biological_rationale: 结构感知稳定性和力场能量需要结构输入
  instruction: |
    若后续需要结构评估且没有现成结构 URL，可调用 esmf_predict_protein_structure
    或 omegafold_predict_structure。保留返回的结构文件 URL；不要把预测结构等同于实验结构。
  recommended_tools:
  - esmf_predict_protein_structure
  - omegafold_predict_structure
  required: false
- step_id: structure_backed_screening
  title: 结构级稳定性检查
  goal: 补充结构感知稳定性和物理能量证据
  biological_rationale: 序列模型和结构模型互补；结构能量可发现明显几何或相互作用风险
  instruction: |
    R7 结构漏斗使用两个矩阵级工具完成。step 3.5 调用
    fold_mutation_matrix_structures，对候选和 R6-6H09 baseline 做结构折叠并下载 PDB。
    step 3.6 调用 score_folded_mutation_matrix_structures，从本地 PDB 计算 pLDDT；
    当结构 URL 可用时，再调用 EvoEF2 记录候选、baseline 和 delta 能量。
    远程 URL 只服务 EvoEF2，本地 PDB 才是 pLDDT 的输入真值。
    若该步骤需要生成最终 Top 5 artifact，可使用 write_r7_top5_auto_artifacts
    从结构复核后的 CSV 自动写出 Top 5、理由和 near-miss 文件。
  recommended_tools:
  - fold_mutation_matrix_structures
  - score_folded_mutation_matrix_structures
  - write_r7_top5_auto_artifacts
  - evoef2_calculate_energy
  - compute_structure_energy
  required: false
---

# Protein Developability Screening

## Overview

This skill screens protein candidates with the real registered BioAgent tools ported from
the research toolbox. It supports sequence-level Tm, thermophilic classification, pH,
solubility/usability, and structure-backed stability or energy checks.

## Interpretation

- PPTStab Tm: predicted melting temperature in Celsius; higher is usually better.
- DeepTP: thermophilic/heat-tolerant classification score; higher positive confidence is better.
- EpHod: predicted optimal pH; compare to the intended operating condition, not simply high/low.
- NetSolP: solubility and usability scores from 0 to 1; higher is better.
- pLDDT: confidence proxy read from predicted PDB B-factors; compare candidates with the same predicted baseline.
- EvoEF2: structure-dependent energy evidence; use candidate-minus-baseline delta rather than absolute values alone.

## Common Pitfalls

- Do not treat model-predicted Tm as experimental Tm.
- Do not compare structure energy across unrelated modeling settings without normalization.
- If an API call fails or times out, report failure status instead of inventing values.
