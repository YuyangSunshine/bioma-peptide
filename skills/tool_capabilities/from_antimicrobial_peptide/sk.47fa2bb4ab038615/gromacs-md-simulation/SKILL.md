---
name: gromacs-molecular-dynamics
description: GROMACS 分子动力学模拟：高效的分子动力学引擎，支持蛋白质、肽段、膜系统等多种体系。 包括拓扑生成（pdb2gmx）、盒子设置（editconf）、溶剂化（solvate）、能量最小化、 NVT/NPT 平衡、生产动力学模拟。用于肽段稳定性评估、折叠预测、功能机制研究。
license: MIT
metadata:
  skill-author: BioAgent Community
  domain: computational-chemistry
  version: 1.0.0
  status: active
routing:
  keywords:
  - GROMACS
  - molecular dynamics
  - MD simulation
  - peptide simulation
  - protein dynamics
  - protein folding
  - membrane systems
  tags:
  - simulation
  - structural-dynamics
  - physics
tools: []
entry_conditions:
- 用户有蛋白质/肽段 PDB 结构
- 需要进行分子动力学模拟
- 需要评估蛋白质稳定性或动力学行为
output_expectations:
- 动力学轨迹文件（.trr, .xtc）
- 结构文件（.gro, .pdb）
- 能量数据（.edr）
- 分析结果（RMSD, Rg, etc.）
---

# GROMACS: Molecular Dynamics Simulation

## Overview

GROMACS (GROningen MAchine for Chemical Simulations) 是全球最广泛使用的开源分子动力学模拟软件。
具有高效率（一般比 NAMD 快 2~3 倍）、对肽段友好的力场支持（CHARMM36m）、完整的模拟流程。

此技能提供：
1. **拓扑和参数**：生成蛋白质/肽段拓扑（pdb2gmx + CHARMM36m）
2. **体系准备**：定义模拟盒子、添加溶剂和离子
3. **模拟平衡**：能量最小化、NVT 加热、NPT 平衡
4. **生产模拟**：NPT 制约条件下的 MD 动力学
5. **轨迹分析**：RMSD、回旋半径、二级结构变化等

## When to Use This Skill

- **AMP 稳定性评估**：肽段在水溶液中的折叠稳定性
- **肽段结构优化**：几何优化、氢键网络分析
- **膜相互作用**：AMP 与细菌膜（双分子层）的作用模式
- **分子机制研究**：受体结合、蛋白-蛋白相互作用
- **力场验证**：验证 AlphaFold/RoseTTAFold 预测的稳定性

**计算要求：** 短肽段 (10~50 aa) 需 1~4 天（GPU）；大蛋白 (100+ aa) 需数周

## Quick Start

```bash
# 1. 安装 GROMACS
conda install -c bioconda gromacs

# 2. 准备输入结构（已有 PDB）
wget https://files.rcsb.org/download/1MB3.pdb  # 示例：肽段 PDB

# 3. 生成拓扑（使用 CHARMM36m 力场）
gmx pdb2gmx -f peptide.pdb -o peptide_processed.gro \
  -p peptide.top -i peptide.itp \
  -ff charmm36 -water tip3p -ignh

# 4. 定义盒子
gmx editconf -f peptide_processed.gro -o peptide_box.gro \
  -c -d 1.0 -bt cubic

# 5. 溶剂化（添加水）
gmx solvate -cp peptide_box.gro -cs spc216.gro \
  -o peptide_solv.gro -p peptide.top

# 6. 中和系统（添加 Cl-/Na+ 离子）
gmx grompp -f ions.mdp -c peptide_solv.gro \
  -p peptide.top -o ions.tpr -v

gmx genion -s ions.tpr -o peptide_neutral.gro \
  -p peptide.top -pname NA -nname CL -neutral

# 7. 能量最小化
gmx grompp -f minim.mdp -c peptide_neutral.gro \
  -p peptide.top -o em.tpr

gmx mdrun -v -deffnm em

# 8. NVT 加热平衡
gmx grompp -f nvt.mdp -c em.gro -p peptide.top -o nvt.tpr
gmx mdrun -v -deffnm nvt

# 9. NPT 压力平衡
gmx grompp -f npt.mdp -c nvt.gro -p peptide.top -o npt.tpr
gmx mdrun -v -deffnm npt

# 10. 生产动力学（100 ns）
gmx grompp -f md.mdp -c npt.gro -p peptide.top -o md.tpr
gmx mdrun -v -deffnm md -gpu_id 0  # 用 GPU 加速

# 11. 分析轨迹
gmx rms -s md.tpr -f md.xtc -o rmsd.xvg
gmx gyrate -s md.tpr -f md.xtc -o gyrate.xvg
gmx hbond -s md.tpr -f md.xtc -o hbond.xvg
```

## Core Capabilities

### 1. 拓扑生成（pdb2gmx）

```bash
# 使用 CHARMM36m 力场（推荐用于肽段和蛋白）
gmx pdb2gmx \
  -f structure.pdb \
  -o structure_processed.gro \
  -p topology.top \
  -i structure.itp \
  -ff charmm36 \         # 力场
  -water tip3p \         # 水模型
  -ignh \                # 忽略 H 原子，自动添加
  -ter \                 # 交互式设置末端
  -vsite hydrogens       # 虚拟位点加速

# 力场选择：
# - charmm36：最通用、最精确（推荐）
# - amber99sb：AMBER 力场（快）
# - oplsaa：OPLS-AA 力场
```

**输出文件：**
- `structure_processed.gro`：处理后结构坐标
- `topology.top`：分子拓扑（键、角、二面角参数）
- `structure.itp`：蛋白质部分拓扑（可修改）

### 2. 模拟盒子设置

```bash
# 定义模拟盒子（周期边界条件）
gmx editconf \
  -f structure.gro \
  -o structure_box.gro \
  -c \                   # 蛋白质居中
  -d 1.0 \               # 距离盒子边界 1.0 nm
  -bt cubic              # 盒子类型（cubic, dodecahedron, octahedron）

# 显式溶剂化（加水）
gmx solvate \
  -cp structure_box.gro \
  -cs spc216.gro \       # SPC 水结构（预定义）
  -o structure_solv.gro \
  -p topology.top

# 中和系统（平衡电荷）
# 先运行离子生成 grompp：
gmx grompp -f ions.mdp -c structure_solv.gro \
  -p topology.top -o ions.tpr

# 使用 genion 添加相反电荷离子
gmx genion \
  -s ions.tpr \
  -o structure_neutral.gro \
  -p topology.top \
  -pname NA -nname CL \  # 阳离子 Na+、阴离子 Cl-
  -neutral \             # 恰好中和（可选：-conc 0.15 加生理盐浓度）
  << EOF
15  # 选择溶剂组（通常是水）
EOF
```

### 3. 能量最小化（EM）

**minim.mdp 配置：**

```ini
; Minimization parameters
integrator              = steep     ; Steepest descent
nsteps                  = 50000     ; 最大迭代步
emtol                   = 1000      ; 力收敛标准 (kJ/mol/nm)
emstep                  = 0.01      ; 初始步长 (nm)

; 力场参数
nstlist                 = 1
ns_type                 = simple
rlist                   = 1.0
rcoulomb                = 1.0
rvdw                    = 1.0
coulombtype             = PME
pme_order               = 4
ewald_rtol              = 1e-5

; 约束
constraints             = h-bonds   ; 固定 H 键
constraint_algorithm    = lincs
```

```bash
gmx grompp -f minim.mdp -c structure_neutral.gro \
  -p topology.top -o em.tpr

gmx mdrun -v -deffnm em -ntmpi 1
```

### 4. NVT 加热平衡（T 恒定体积）

**nvt.mdp：**

```ini
title                   = NVT equilibration
define                  = -DPOSRES  ; 位置限制（蛋白固定）

integrator              = md        ; Verlet 积分器
dt                      = 0.002     ; 时间步 2 fs
nsteps                  = 50000     ; 100 ps 总时间

tcoupl                  = V-rescale ; 温度耦合
tc_grps                 = Protein Non-Protein
tau_t                   = 0.1 0.1
ref_t                   = 300 300   ; 目标温度 300 K

pcoupl                  = no        ; 无压力耦合

nstxout                 = 0
nstv                    = 0
nstfout                 = 0
nstlog                  = 1000      ; 日志频率
nstenergy               = 1000
nstxtcout               = 1000      ; 轨迹存储

constraints             = h-bonds
constraint_algorithm    = lincs
continuation            = no        ; 从 EM 开始（非续行）
```

```bash
gmx grompp -f nvt.mdp -c em.gro -p topology.top \
  -r em.gro -o nvt.tpr

gmx mdrun -v -deffnm nvt
```

### 5. NPT 压力平衡（P/T 恒定）

**npt.mdp：**

```ini
title                   = NPT equilibration
define                  = -DPOSRES

integrator              = md
dt                      = 0.002
nsteps                  = 50000      ; 100 ps

tcoupl                  = V-rescale
tc_grps                 = Protein Non-Protein
tau_t                   = 0.1 0.1
ref_t                   = 300 300

pcoupl                  = Parrinello-Rahman  ; 压力耦合
pcoupltype              = isotropic
tau_p                   = 2.0
ref_p                   = 1.0        ; 1 bar
compressibility         = 4.5e-5     ; 水的压缩系数

nstlog                  = 1000
nstenergy               = 1000
nstxtcout               = 1000

constraints             = h-bonds
constraint_algorithm    = lincs
continuation            = yes        ; 从 NVT 续行（保留速度）
```

### 6. 生产动力学模拟

**md.mdp（生产运行配置）：**

```ini
title                   = Production MD
define                  =            ; 无位置限制

integrator              = md
dt                      = 0.002      ; 2 fs
nsteps                  = 50000000   ; 100 ns 总长度

tcoupl                  = V-rescale
tc_grps                 = Protein Non-Protein
tau_t                   = 0.1 0.1
ref_t                   = 300 300

pcoupl                  = Parrinello-Rahman
pcoupltype              = isotropic
tau_p                   = 2.0
ref_p                   = 1.0
compressibility         = 4.5e-5

nstlog                  = 5000
nstenergy               = 5000
nstxout_compressed      = 5000       ; 压缩轨迹（每 10 ps）

constraints             = h-bonds
```

```bash
# 生产运行（GPU 加速）
gmx mdrun -v -deffnm md -gpu_id 0 -ntomp 4
```

### 7. 轨迹分析

```bash
# RMSD（均方根偏差）- 蛋白质稳定性指标
gmx rms -s md.tpr -f md.xtc -o rmsd.xvg -tu ns

# 回旋半径（Rg）- 蛋白质展开度
gmx gyrate -s md.tpr -f md.xtc -o gyrate.xvg

# 氢键分析
gmx hbond -s md.tpr -f md.xtc -o hbond.xvg -num hbond_num.xvg

# 二级结构变化（DSSP）
gmx do_dssp -s md.tpr -f md.xtc -o ss.xpm -tu ns

# Python 解析 XVG 文件
import pandas as pd

df = pd.read_csv("rmsd.xvg", comment="@", delim_whitespace=True, header=None)
print(df.head())
# 绘图
import matplotlib.pyplot as plt
plt.plot(df[0], df[1])
plt.xlabel("Time (ns)")
plt.ylabel("RMSD (nm)")
plt.savefig("rmsd_plot.png", dpi=300)
```

## Parameters Reference

| 参数 | 说明 | 推荐值 |
|------|------|--------|
| `dt` | 时间步 | 2 fs |
| `nsteps` | 模拟步数 | 50,000 (100 ps) ~ 5,000,000 (10 ns) |
| `tcoupl` | 温度耦合 | V-rescale |
| `ref_t` | 目标温度 | 300 K |
| `pcoupl` | 压力耦合 | Parrinello-Rahman |
| `ref_p` | 目标压力 | 1 bar |
| `constraints` | 约束 | h-bonds |

## Common Pitfalls

- **力场选择**：CHARMM36m 最精确但计算量大；AMBER 更快但精度略低
- **盒子大小**：太小导致周期性影响；太大浪费计算；通常 1.0~2.0 nm 边距
- **模拟时间过短**：<100 ns 不足以评估稳定性；短肽段需 50~100 ns
- **约束选择**：`h-bonds` 允许 2 fs 时间步；`all-bonds` 减少约束但需更小时间步
- **GPU 加速遗漏**：用 `-gpu_id` 指定 GPU 可加速 10~50 倍

## References

- [GROMACS 官方文档](https://manual.gromacs.org/)
- [CHARMM36m 力场论文](https://doi.org/10.1021/acs.jcim.7b00698)
- [GROMACS 教程](https://www.gromacs.org/Documentation)
