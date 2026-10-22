---
name: sequence-level-dna-analysis
description: DNA/RNA 序列级分析技能：计算 GC 含量、识别开放阅读框（ORF）、 翻译核苷酸序列为蛋白质序列、分析密码子使用偏好、查询氨基酸理化性质。 适用于基因功能分析、密码子优化和序列特征提取。
license: Apache-2.0
metadata:
  skill-author: PredAgent Team
  domain: genomics
  version: 1.0.0
  status: active
routing:
  keywords:
  - DNA sequence
  - RNA
  - GC content
  - ORF
  - open reading frame
  - translation
  - codon usage
  - codon optimization
  - nucleotide
  tags:
  - genomics
  - molecular-biology
tools:
- compute_gc_content
- find_open_reading_frames
- translate_dna_to_protein
- analyze_codon_usage
- get_amino_acid_properties
- reverse_complement
- extract_sequence_from_fasta
- find_longest_orf
- translate_dna
- calc_translation_efficiency
- amino_acid_at_position
- amino_acid_in_longest_orf
- amino_acid_in_longest_orf_six_frame
entry_conditions:
- 用户提供了 DNA 或 RNA 序列（字符串或 FASTA 格式）
output_expectations:
- GC 含量百分比
- ORF 列表（起始位置、终止位置、翻译序列）
- 密码子使用频率表
procedure:
- step_id: sequence_stats
  title: 序列基本统计
  goal: 计算 GC 含量等基础序列特征
  biological_rationale: GC 含量影响 DNA 双链稳定性和基因调控
  instruction: |
    使用 compute_gc_content 计算整体和局部 GC 含量。
    对于 mRNA，检查 5'UTR 的 GC 含量（影响翻译效率）。
  recommended_tools:
  - compute_gc_content
  - reverse_complement
  - extract_sequence_from_fasta
  required: true
- step_id: orf_detection
  title: 开放阅读框识别
  goal: 找出所有可能的蛋白质编码区域
  biological_rationale: ORF 是基因功能注释的基础
  instruction: |
    使用 find_open_reading_frames 在所有 6 个阅读框中搜索 ATG→Stop。
    返回长度 > 100 nt 的 ORF（过滤随机短 ORF）。
  recommended_tools:
  - find_open_reading_frames
  - find_longest_orf
  required: true
- step_id: translation
  title: 序列翻译
  goal: 将核苷酸序列翻译为氨基酸序列
  biological_rationale: 蛋白质序列是功能注释的直接对象
  instruction: |
    使用 translate_dna_to_protein 翻译选定 ORF。
    使用标准遗传密码表（NCBI table 1）。
  recommended_tools:
  - translate_dna_to_protein
  - translate_dna
  - amino_acid_at_position
  - amino_acid_in_longest_orf
  - amino_acid_in_longest_orf_six_frame
  required: false
- step_id: codon_analysis
  title: 密码子使用分析
  goal: 分析密码子偏好性以辅助表达载体设计
  biological_rationale: 密码子使用偏好影响宿主系统中的蛋白质表达水平
  instruction: |
    使用 analyze_codon_usage 计算每个氨基酸的密码子使用频率。
    与大肠杆菌/酵母/CHO 细胞的偏好密码子表对比。
  recommended_tools:
  - analyze_codon_usage
  - calc_translation_efficiency
  required: false
---

# Sequence-Level DNA Analysis

## Overview

This skill handles fundamental DNA/RNA sequence analysis operations: GC content calculation,
open reading frame detection, translation, and codon usage analysis. These are foundational
tools for molecular biology, synthetic biology, and gene annotation workflows.

## When to Use This Skill

- Characterizing novel DNA sequences from sequencing data
- Finding protein-coding regions in genomic sequences
- Translating gene sequences to predict protein products
- Optimizing codon usage for heterologous expression
- Calculating primer binding characteristics

## Quick Start

```python
from bioagent.tools import compute_gc_content, find_open_reading_frames

seq = "ATGAAACGCATTAGCACCACCATTACCACCACCATCACCATTACCACAGGT"
gc = compute_gc_content(seq)  # Returns: 35.4
orfs = find_open_reading_frames(seq, min_length=30)
```

## Reading Frames

DNA has 6 possible reading frames: 3 forward (+1, +2, +3) and 3 reverse complement (-1, -2, -3).

## Common Pitfalls

- RNA sequences use U instead of T — tools accept both
- Circular genomes may have ORFs spanning the linearization point
- Stop codons in alternative genetic codes differ (mitochondrial, etc.)
