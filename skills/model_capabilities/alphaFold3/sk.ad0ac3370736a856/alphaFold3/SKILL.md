---
name: alphafold3
description: '使用 AlphaFold3 预测蛋白质及其复合物的三维结构。AF3 是 DeepMind 2024年发布的新一代 蛋白质结构预测模型，支持蛋白、核酸(DNA/RNA)、小分子配体、金属离子、共价修饰等全原子复合物预测， 准确性远超 AlphaFold2。通过扩散模型直接生成原子坐标，无需MSA也可获得高质量结构。 Use when: user needs to predict protein/ligand/nucleic acid complex structures, protein-ligand docking, protein-protein interactions, post-translational modifications.'
license: CC-BY-NC-SA-4.0 (model weights)
metadata:
  skill-author: BioAgent Community
  domain: structural-biology
  version: 1.0.0
  status: active
tools: []
tags:
- alphafold3
- protein-structure-prediction
- diffusion-model
- docking
- complex-prediction
---

# AlphaFold3 — 蛋白质复合物结构预测

## Overview

AlphaFold3 (AF3) 是 Google DeepMind 在 2024 年 11 月发布的第三代蛋白质结构预测模型。
与前代 AF2 相比，AF3 的核心突破是**去掉了对 MSA 的强依赖**，改用**扩散模型 (Diffusion Model)**
直接从初始猜测生成原子坐标，扩展到**全原子复合物**（蛋白质、核酸、配体、离子、共价修饰）的联合预测。

> **论文**: Abramson et al., *Nature* (2024). "Accurate structure prediction of biomolecular interactions with AlphaFold 3"
> **代码/权重**: https://github.com/google-deepmind/alphafold3 (CC-BY-NC-SA-4.0)

## How AlphaFold3 Works

### 1. 架构总览

AF3 的整体 flow:

```
Input Sequence(s) + Ligand SMILES/Ions
        │
        ▼
  ┌─────────────────┐
  │  Template/Input  │  ← 可选: MSA (不必须), templates
  │  Featureizer     │
  └────────┬────────┘
           │ tokenized representation
           ▼
  ┌─────────────────┐
  │  Pairformer      │  ← Transformer-based module
  │  (48 blocks)     │     pair representation + single representation
  └────────┬────────┘
           │ refined pairwise + single features
           ▼
  ┌─────────────────┐
  │  Diffusion Head  │  ← 核心创新: 扩散模型去噪
  │  (generative)    │     从 white-noise 初始坐标逐步"去噪"生成原子坐标
  └────────┬────────┘
           │ raw atom coordinates
           ▼
  ┌─────────────────┐
  │  Confidence Head │  ← pLDDT / PAE / ipTM 评分
  └────────┬────────┘
           ▼
    Final 3D structure + confidence metrics
```

### 2. 核心模块详解

#### a) Input Featureizer

- **不需要 MSA**: AF3 可以完全不使用多序列比对 (MSA)，仅从单体序列出发也能得到高质量预测。
  如果有 MSA，作为额外信息源使用（但不是必需的）。
- **多模态输入**: 一条或多条蛋白质序列 (FASTA)、DNA/RNA 序列、SMILES 格式配体、金属离子 (Zn²⁺/Mg²⁺ 等)、
  残基共价修饰（磷酸化、乙酰化等）统一 tokenize。
- **Template**: 可选的同源模板（PDB 结构），作为结构先验嵌入。

**关键改变**: AF2 几乎必须用 MSA 做共进化分析 (co-evolution)；AF3 完全放弃了这个假设，
通过扩散模型学到的物理-化学约束替代共进化信号。

#### b) Pairformer (取代 AF2 的 Evoformer)

- **48 个 Transformer block** 组成。
- 维护两个核心表示:
  - **Single representation** (每个残基/原子): 编码该位置的化学环境。
  - **Pair representation** (每对残基/原子): 编码相对空间关系 (距离/方向/相互作用)。
- Pairformer 只更新 pair→single 的单向信息流 (相比 AF2 的 Evoformer 简化为单向)，
  显著降低显存占用。
- 输出: 一个高度精炼的 pairwise 特征矩阵 + single 特征。

#### c) Diffusion Head (扩散模块) ⭐ 核心创新

这是 AF3 与 AF2 最本质的区别：

```
Training:                               Inference:
真实结构 + 逐步加噪声                   随机噪声坐标
    │                                       │
    ▼                                       ▼
噪声坐标 (t steps)                    噪声坐标
    │                                       │
    ▼ (denoise network)                 ▼ (denoise network)
预测去噪方向                           预测去噪方向
    │                                       │
    ▼ (loss vs ground-truth)             ▼ (iterative denoising)
梯度回传                                最终原子坐标
```

- **Diffusion model** 的本质: 在训练时学习"如何从噪声中恢复结构"——
  给模型看一个带噪声的坐标 $x_t = \sqrt{\bar{\alpha}_t} x_0 + \sqrt{1-\bar{\alpha}_t} \epsilon$，
  让它预测原始干净坐标 $x_0$（或噪声 $\epsilon$）。
- **推理时**: 从纯高斯噪声出发，通过 200 步 iterative denoising 逐步生成原子坐标。
- **为什么可以不用 MSA?** 扩散模型学到了蛋白质折叠的"物理规则"——凡·德瓦力、氢键、疏水作用——
  而不是依赖共进化统计，因此对孤儿蛋白/设计蛋白同样有效。

#### d) Confidence Head

- **pLDDT (predicted Local Distance Difference Test)**: 每个残基的置信度 (0-100)，>90 为高置信。
- **PAE (Predicted Aligned Error)**: 残基对之间的预期位置误差矩阵，用于评估域间相对位置可靠性。
- **ipTM (interface predicted TM-score)**: 蛋白-蛋白复合物界面质量的评估指标，>0.8 说明界面预测可靠。

### 3. AF3 vs AF2 对比

| 特性 | AlphaFold2 | AlphaFold3 |
|------|-----------|-----------|
| **核心架构** | Evoformer + Structure Module | Pairformer + Diffusion Head |
| **需 MSA** | **必需** (高依赖) | **可选** (弱依赖) |
| **原子表示** | 残基级 Cα (主链) | 全原子 (所有重原子) |
| **支持分子** | 仅蛋白 | 蛋白 + DNA/RNA + 配体 + 离子 + 共价修饰 |
| **蛋白复合物** | AlphaFold-Multimer (v2.3) | 原生支持多链 + 配体 |
| **蛋白-配体对接** | ❌ 不支持 | ✅ 原生支持 |
| **翻译后修饰** | ❌ 不支持 | ✅ 支持 (磷酸化/乙酰化等) |
| **训练数据** | PDB (~190K) | PDB + 合成数据 (>500K) |
| **模型权重** | Apache 2.0 开源 | CC-BY-NC-SA-4.0 (科研可用) |
| **显存要求** | ~2.5GB (单体) | ~8-16GB (GPU 推荐) |
| **推理速度** | 分钟级 | 分钟级 (扩散200步) |

### 4. 实际使用方法

#### Python API (AlphaFold3 服务)

```python
# 官方推荐途径: AlphaFold Server API 或本地安装
# 1. 通过 AlphaFold Server (免费)
# 访问: https://alphafoldserver.com/
# 限制: 每天 20 个 job, 每个最多 5000 tokens

# 2. 本地运行 (需要 GPU)
# git clone https://github.com/google-deepmind/alphafold3
# 下载模型权重 (~1.2GB), 数据库 (~250GB)
# 运行:
# python run_alphafold.py \
#     --json_path=input.json \
#     --model_dir=models \
#     --output_dir=output
```

#### 输入 JSON 格式

```json
{
  "name": "kinase-inhibitor-complex",
  "modelSeeds": [1],
  "sequences": [
    {
      "protein": {
        "id": "A",
        "sequence": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNAVAHVDDMPNALSALSDLHAHKLRVDPVNFKLLSHCLLVTLAAHLPAEFTPAVHASLDKFLASVSTVLTSKYR"
      }
    },
    {
      "ligand": {
        "id": "B",
        "smiles": "CC1=C(C(=O)N(N1C)C2=CC=CC=C2)CN3CCN(CC3)C4=CC=CC=C4",
        "ccdCodes": null
      }
    },
    {
      "ion": {
        "id": "C",
        "ion": "MG"
      }
    }
  ],
  "bondedAtomPairs": [],
  "userCCD": "",
  "dialect": "alphafold3",
  "version": 2
}
```

#### 输出解读

```
output/
├── {name}_model_0.cif          # 3D 结构 (mmCIF格式，可用PyMOL/ChimeraX打开)
├── {name}_summary_confidences.json  # 全局置信度
│   ├── plddt: 92.5             # 平均 pLDDT
│   ├── ptm: 0.91               # 整体 TM-score
│   ├── iptm: 0.85              # 界面 TM-score
│   └── fraction_disordered: 0.02
└── {name}_confidences.json     # 逐残基置信度
    ├── plddt: [95, 93, 88, ...]
    ├── pae: [[0, 1.2, 3.5, ...], ...]
    └── token_chain_ids + atom_chain_ids
```

### 5. 常见应用场景

| 场景 | AF3 表现 |
|------|---------|
| 单体蛋白折叠 | ⭐⭐⭐⭐⭐ 精度与 AF2 相当或更优 |
| 蛋白-蛋白复合物 | ⭐⭐⭐⭐⭐ 原生支持，界面精度显著优于 AF2-Multimer |
| 蛋白-配体对接 | ⭐⭐⭐⭐ 接近 AutoDock Vina 水平，速度快 |
| 蛋白-DNA/RNA | ⭐⭐⭐⭐ 准确预测核酸结合界面 |
| 抗体-抗原 | ⭐⭐⭐⭐ 可预测 CDR 环构象 + 界面 |
| 共价修饰 (PTM) | ⭐⭐⭐ 支持磷酸化等，但训练数据有限 |
| 虚拟筛选 | ⭐⭐⭐ 不如专用对接软件，但适合初始筛选 |

### 6. 限制与注意事项

- **许可**: 模型权重为 CC-BY-NC-SA-4.0，科研可免费使用，商业需授权。
- **显存**: 建议至少 16GB GPU 显存 (A100 优化最好，但 V100/A10 也可)。
- **数据库**: 本地运行需要遗传数据库 (~250GB SSD)。
- **配体覆盖**: 并非所有化学空间都被训练覆盖，新骨架化合物可能不准确。
- **动态行为**: 预测的是静态结构，不包含构象动态/柔性信息。

## Quick Reference

| 命令 | 用途 |
|------|------|
| `python run_alphafold.py --json_path=input.json` | 预测结构 |
| `https://alphafoldserver.com/` | 在线免费预测 (每日20次) |
| pLDDT > 90 | 高置信结构区域 |
| ipTM > 0.8 | 蛋白-蛋白界面可靠 |
| PAE < 5 Å | 残基对相对位置可靠 |
