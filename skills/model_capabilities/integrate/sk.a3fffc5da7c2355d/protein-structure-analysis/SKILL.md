---
name: bio-protein-structure-analysis
description: '蛋白质结构分析。Comprehensive protein structure tools: prediction (AlphaFold3/ESMFold/Chai-1), structure I/O (PDB/mmCIF), geometric calculations, modification, navigation, and remote homology detection.'
tools: []
tool_type: analysis
category: 生物/分析计算
---

# 蛋白质结构分析
# Protein Structure Analysis

## Overview

Comprehensive protein structure tools: prediction (AlphaFold3/ESMFold/Chai-1), structure I/O (PDB/mmCIF), geometric calculations, modification, navigation, and remote homology detection.

## Integrated Skills

This skill integrates the following specialized skills:

- `bioskills/alphafold-predictions`: 用AlphaFold预测蛋白质3D结构及解读
- `bioskills/modern-structure-prediction`: 用AlphaFold3/ESMFold/Chai-1/Boltz-1预测蛋白质结构
- `bioskills/structure-io`: 用Bio.PDB读写蛋白结构文件（PDB/mmCIF/MMTF）
- `bioskills/structure-modification`: 用Bio.PDB修改蛋白结构（坐标变换、原子/残基删改）
- `bioskills/structure-navigation`: 用Bio.PDB SMCRA模型遍历蛋白结构层级（模型/链/残基/原子）
- `bioskills/geometric-analysis`: 用Bio.PDB做蛋白质结构几何计算（距离、角度、二面角）
- `bioskills/remote-homology`: 用HMM/profile方法检测远缘同源（超越BLAST）

## Quick Reference

| Sub-skill | Purpose |
|-----------|---------|
| `alphafold-predictions` | 用AlphaFold预测蛋白质3D结构及解读 |
| `modern-structure-prediction` | 用AlphaFold3/ESMFold/Chai-1/Boltz-1预测蛋白质结构 |
| `structure-io` | 用Bio.PDB读写蛋白结构文件（PDB/mmCIF/MMTF） |
| `structure-modification` | 用Bio.PDB修改蛋白结构（坐标变换、原子/残基删改） |
| `structure-navigation` | 用Bio.PDB SMCRA模型遍历蛋白结构层级（模型/链/残基/原子） |
| `geometric-analysis` | 用Bio.PDB做蛋白质结构几何计算（距离、角度、二面角） |
| `remote-homology` | 用HMM/profile方法检测远缘同源（超越BLAST） |

## Usage

Choose the appropriate sub-skill based on your specific analysis needs.
For an end-to-end workflow, follow the pipeline sub-skill (if available) which orchestrates all steps.
