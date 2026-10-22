---
name: bio-sequence-alignment
description: 序列比对与质控。Use for pairwise DNA/protein alignment, MAFFT/MUSCLE multiple-sequence alignment, DNA read mapping with BWA-MEM2, splice-aware RNA-seq alignment with STAR, and alignment summary or quality review.
tools:
- bio_sequence_alignment
- sciagent_run_bwa_mem2
- sciagent_run_star
- sciagent_inspect_pysam
tool_type: analysis
category: 生物/分析计算
---

# 序列比对与质控
# Sequence Alignment

## Overview

Perform read alignment (BWA/Bowtie2/STAR), post-processing (sorting/indexing/filtering/clipping), and quality validation using samtools, pysam, and Bio.AlignIO.

## Integrated Skills

This skill integrates the following specialized skills:

- `bioskills/alignment-amplicon-clipping`: 用samtools ampliconclip去除扩增子引物序列
- `bioskills/alignment-filtering`: 用samtools view/pysam按条件过滤比对结果
- `bioskills/alignment-indexing`: 用samtools/pysam为BAM/CRAM文件创建索引
- `bioskills/alignment-io`: 用Bio.AlignIO读写多种多序列比对格式
- `bioskills/alignment-sorting`: 用samtools/pysam排序比对文件
- `bioskills/alignment-trimming`: 用ClipKIT/trimAl/BMGE修剪多序列比对不可靠列
- `bioskills/alignment-validation`: 评估比对质量（插入片段分布、GC偏倚、链平衡等）

## Quick Reference

| Sub-skill | Purpose |
|-----------|---------|
| `alignment-amplicon-clipping` | 用samtools ampliconclip去除扩增子引物序列 |
| `alignment-filtering` | 用samtools view/pysam按条件过滤比对结果 |
| `alignment-indexing` | 用samtools/pysam为BAM/CRAM文件创建索引 |
| `alignment-io` | 用Bio.AlignIO读写多种多序列比对格式 |
| `alignment-sorting` | 用samtools/pysam排序比对文件 |
| `alignment-trimming` | 用ClipKIT/trimAl/BMGE修剪多序列比对不可靠列 |
| `alignment-validation` | 评估比对质量（插入片段分布、GC偏倚、链平衡等） |

## Usage

Read this Skill before selecting a tool. Choose `bio_sequence_alignment` for pairwise or multiple sequence alignment, `sciagent_run_bwa_mem2` for genomic DNA FASTQ mapping, `sciagent_run_star` for splice-aware RNA-seq mapping, and `sciagent_inspect_pysam` for FASTA/FASTQ/SAM/BAM inspection. Build the required reference index before calling a read aligner.
