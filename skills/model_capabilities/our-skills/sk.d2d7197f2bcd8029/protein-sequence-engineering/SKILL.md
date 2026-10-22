---
name: protein-sequence-engineering
description: 蛋白质序列工程技能：从 UniProt 获取蛋白质序列，构建突变体，使用 ESM-2 进行 突变效应预测（log-likelihood ratio），分析蛋白质接触图。适用于突变扫描、 定向进化和功能预测场景。
license: Apache-2.0
metadata:
  skill-author: PredAgent Team
  domain: proteomics
  version: 1.0.0
  status: active
routing:
  keywords:
  - protein engineering
  - mutation scoring
  - ESM-2
  - sequence analysis
  - amino acid substitution
  - directed evolution
  - protein function prediction
  tags:
  - proteomics
  - protein-engineering
  - esm
tools:
- fetch_sequence_by_uniprot_id
- normalize_sequence
- analyze_protein_sequence
- apply_mutations_to_sequence
- generate_r7_candidate_combinations
- evaluate_mutations_with_esm2
- evaluate_mutation_matrix_with_esm2
- evaluate_mutation_matrix_with_developability_tools
- filter_r7_candidate_matrix
- write_r7_top5_auto_artifacts
- write_r7_top5_artifacts
- predict_protein_contacts_with_esm2
- extract_protein_embeddings_with_esm2
- get_amino_acid_properties
entry_conditions:
- 用户提供了蛋白质序列或 UniProt ID
- 用户指定了需要分析的突变位点（如 R175H）
- 用户提供了包含 full_sequence 和 mutations_vs_backbone 的突变矩阵 CSV
output_expectations:
- R7 step 3.1 候选突变组合 CSV（由 generate_r7_candidate_combinations 生成，避免 LLM 手写大 CSV）
- 每个突变的 ESM-2 log-likelihood ratio 评分
- 突变矩阵的候选级 masked-marginal LLR、contextual LLR 和 epistasis delta
- 突变前后的序列比较
- 接触图热图（如需要）
procedure:
- step_id: fetch_sequence
  title: 获取蛋白质序列
  goal: 从 UniProt 获取目标蛋白质的氨基酸序列
  biological_rationale: 所有下游分析需要准确的野生型序列作为基准
  instruction: |
    若用户提供了 UniProt ID（如 P04637），使用 fetch_sequence_by_uniprot_id 获取序列。
    若用户已提供序列，跳过此步骤并使用 normalize_sequence 标准化。
  recommended_tools:
  - fetch_sequence_by_uniprot_id
  - normalize_sequence
  required: false
- step_id: construct_mutations
  title: 构建突变体
  goal: 将用户指定的突变应用到野生型序列上
  biological_rationale: 需要生成突变体序列用于下游功能预测
  instruction: |
    使用 apply_mutations_to_sequence 工具。
    突变格式为 A123G（原始氨基酸+位置+新氨基酸）。
    支持多点突变，以逗号分隔。
  recommended_tools:
  - apply_mutations_to_sequence
  required: true
- step_id: score_mutations
  title: ESM-2 突变评分
  goal: 使用 ESM-2 语言模型预测突变的功能影响
  biological_rationale: ESM-2 masked-marginal log-likelihood ratio 可作为序列相容性 proxy，但不是 Tm 或 ΔΔG 预测
  instruction: |
    对单条序列和少量突变，使用 evaluate_mutations_with_esm2 进行评分。
    对包含 candidate_id、mutations_vs_backbone、full_sequence 的候选矩阵，
    使用 evaluate_mutation_matrix_with_esm2 一次性批量评分，不要选代表性候选后
    把同一个 ESM 结果复制到全部候选。
    矩阵工具会输出 additive LLR、candidate-specific contextual LLR 和
    epistasis_delta=contextual-additive；epistasis_delta 只作为多点突变相容性 proxy。
    如果 ESM-2 不可用，不得生成 simulated scores。
  recommended_tools:
  - evaluate_mutations_with_esm2
  - evaluate_mutation_matrix_with_esm2
  required: true
- step_id: contact_analysis
  title: 接触图分析（可选）
  goal: 预测蛋白质残基接触图以理解突变位点的结构背景
  biological_rationale: 接触图揭示突变残基与其他残基的相互作用，辅助解释功能影响
  instruction: |
    若用户请求了结构分析，使用 predict_protein_contacts_with_esm2。
    重点关注突变位点附近（±5 个残基）的接触模式。
  recommended_tools:
  - predict_protein_contacts_with_esm2
  - extract_protein_embeddings_with_esm2
  required: false
---

# Protein Sequence Engineering

## Overview

This skill provides a structured workflow for protein sequence analysis and engineering using
ESM-2 protein language models. It enables systematic evaluation of amino acid mutations,
prediction of their functional consequences, and analysis of residue-residue contacts.

The ESM-2 log-likelihood ratio (LLR) metric has been validated against deep mutational
scanning datasets across hundreds of proteins and consistently outperforms BLOSUM-based
substitution matrices for predicting mutational fitness effects.

## When to Use This Skill

- Evaluating the effect of specific amino acid mutations on protein function
- Prioritizing mutations for experimental validation
- Designing protein variants with improved properties
- Understanding structural context of mutations via contact maps
- Scanning all possible single-point mutations at a position (saturation mutagenesis)

## Quick Start

```python
from bioagent.tools import (
    fetch_sequence_by_uniprot_id,
    apply_mutations_to_sequence,
    evaluate_mutations_with_esm2,
    evaluate_mutation_matrix_with_esm2,
    evaluate_mutation_matrix_with_developability_tools,
)

# TP53 tumor suppressor
seq = fetch_sequence_by_uniprot_id("P04637")
mutants = apply_mutations_to_sequence(seq, "R175H,R248W,R273H")
scores = evaluate_mutations_with_esm2(seq, "R175H,R248W,R273H")
# Returns: {"R175H": -3.21, "R248W": -4.87, "R273H": -2.95}

# Candidate matrix scoring
matrix_scores = evaluate_mutation_matrix_with_esm2(
    matrix_csv_path="shishiyan/r7_step3_2_mutation_matrix_raw.csv",
    output_csv_path="shishiyan/r7_step3_3_esm_scored.csv",
    model_name="esm2_t33_650M_UR50D",
)
esm_shortlist = filter_r7_candidate_matrix(
    matrix_csv_path="shishiyan/r7_step3_3_esm_scored.csv",
    output_csv_path="shishiyan/r7_step3_3_esm_shortlist.csv",
    output_summary_md_path="shishiyan/r7_step3_3_esm_shortlist_summary.md",
    stage="esm",
    limit=30,
)
```

## Mutation Format

Mutations follow the standard notation: `{WT_AA}{POSITION}{MUT_AA}`

- Single mutation: `R175H` (Arg→His at position 175)
- Multiple mutations: `R175H,R248W` (comma-separated)
- Deletions: `R175-` (not supported by ESM-2 scoring)

## Interpreting LLR Scores

| LLR Range | Interpretation |
|-----------|----------------|
| > +1.0    | Likely beneficial / functionally tolerated |
| -1.0 to +1.0 | Neutral or uncertain |
| < -1.0    | Likely deleterious |
| < -3.0    | Strongly deleterious (e.g., loss-of-function) |

## Common Pitfalls

- ESM-2 scores reflect evolutionary conservation, not always clinical pathogenicity
- ESM-2 scores are sequence-likelihood proxies, not direct thermal stability or ΔΔG predictions
- Very long sequences (> 1022 aa) may need to be chunked for ESM-2
- Insertions and deletions are not well-captured by LLR scoring
