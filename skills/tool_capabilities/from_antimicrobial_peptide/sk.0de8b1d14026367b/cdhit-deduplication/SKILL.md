---
name: cdhit-deduplication
description: CD-HIT 序列去重：基于相似度阈值（默认 95% 相似度）快速识别和去除冗余序列。 用于宏基因组序列、肽段库、蛋白质数据库的去冗余处理，支持批量处理、聚类统计。
license: MIT
metadata:
  skill-author: BioAgent Community
  domain: genomics
  version: 1.0.0
  status: active
routing:
  keywords:
  - CD-HIT
  - sequence deduplication
  - clustering
  - redundancy removal
  - sequence similarity
  - metagenomics
  tags:
  - preprocessing
  - clustering
  - sequence-processing
tools: []
entry_conditions:
- 用户有大规模序列数据（FASTA 格式）
- 需要按相似度移除冗余序列
- 需要聚类统计相似序列的分组
output_expectations:
- 去重后的序列文件（FASTA）
- 聚类信息（每个序列的代表元和相似度）
- 去重统计报告（原始序列数、去重后序列数、去重率）
---

# CD-HIT: Sequence Deduplication and Clustering

## Overview

CD-HIT (Cluster Database at High Identity with Tolerance) 是一个快速、高效的序列聚类和去重工具。
通过贪心聚类算法，在几分钟内处理百万级序列，按相似度阈值（通常 95%~100%）识别冗余序列并聚类。

此技能提供：
1. **核苷酸序列去重**：DNA/RNA 序列聚类（cd-hit）
2. **蛋白质序列去重**：肽段、蛋白质聚类（cd-hit-est）
3. **2D 聚类**：两阶段聚类加速大规模数据处理
4. **聚类统计**：生成聚类报告、序列映射表
5. **批量处理**：脚本化处理多个文件

## When to Use This Skill

- **宏基因组数据**：去除重复或高相似短 ORF
- **肽段库预处理**：训练 AMP 预测模型前的去冗余
- **蛋白质数据库**：BLAST 数据库构建前的代表序列选择
- **参考基因组构建**：从多物种序列中筛选代表性基因
- **训练数据清理**：机器学习数据集中移除相似样本，避免泄漏

**不适用场景：**
- 需要精确比对的场景（用 BLAST 或 MMseqs2）
- 需要保留所有序列的场景（仅需聚类分析，不需去重）

## Quick Start

```bash
# 1. 安装 CD-HIT
conda install -c bioconda cd-hit

# 2. 蛋白质序列去重（95% 相似度）
cd-hit -i input_protein.fasta -o output_clustered -c 0.95 -n 5 -M 16000 -d 0 -T 8

# 3. 核酸序列去重（95% 相似度）
cd-hit-est -i input_dna.fasta -o output_clustered -c 0.95 -n 10 -M 16000 -d 0 -T 8

# 4. 查看聚类信息
cat output_clustered.clstr

# 5. Python 解析聚类结果
python << 'EOF'
import pandas as pd

# 解析 .clstr 文件
clusters = {}
with open("output_clustered.clstr") as f:
    cluster_id = None
    for line in f:
        if line.startswith(">Cluster"):
            cluster_id = int(line.split()[-1])
            clusters[cluster_id] = []
        else:
            seq_id = line.split(">")[1].split()[0]
            clusters[cluster_id].append(seq_id)

print(f"Total clusters: {len(clusters)}")
print(f"Cluster sizes: {[len(c) for c in clusters.values()]}")
EOF
```

## Core Capabilities

### 1. 基本序列去重

**蛋白质序列去重：**

```bash
# 常见参数组合
cd-hit \
  -i proteins.fasta \        # 输入文件
  -o output \                # 输出前缀
  -c 0.95 \                  # 95% 相似度阈值
  -n 5 \                     # 字长（词长），蛋白质通常 5
  -M 16000 \                 # 最大内存 (MB)
  -T 8 \                     # 线程数
  -d 0                       # 序列描述长度（0=无限）
```

**参数说明：**
- `-c`：序列相似度阈值（0~1），越高越严格，去重少；0.95=95% 相同
- `-n`：字长（分词长度），蛋白 5，核酸 10；越大越快但精度损失
- `-M`：最大内存（MB），超出内存用 2D 聚类
- `-T`：线程数（加速）
- `-d`：序列描述行长度，0 表示不截断

### 2. 核酸序列去重

```bash
# DNA/RNA 序列去重
cd-hit-est \
  -i metagenome_orfs.fasta \
  -o orfs_clustered \
  -c 0.95 \                  # 95% 相似度（对应 >=95% 核苷酸相同）
  -n 10 \                    # 核酸字长更长
  -M 32000 \
  -T 8 \
  -mask NX                   # N/X 字符处理方式
```

**核酸特定参数：**
- `-g`：同义性（0=严格，1=宽松）
- `-aS`：对齐覆盖度（目标序列覆盖率）
- `-aL`：对齐覆盖度（查询序列覆盖率）

### 3. 2D 聚类（处理超大数据集）

```bash
# 第一阶段：粗聚类（80% 相似）
cd-hit -i large_dataset.fasta -o pre_clustered -c 0.8 -n 5 -M 8000 -T 8

# 第二阶段：精细聚类（95% 相似）
cd-hit -i pre_clustered -o final_clustered -c 0.95 -n 5 -M 16000 -T 8

# 合并两阶段聚类结果
cd-hit-2d -i pre_clustered -i2 final_clustered -o result_2d
```

### 4. 聚类统计和可视化

**Python 脚本解析聚类结果：**

```python
import pandas as pd
from collections import defaultdict

def parse_cdhit_clstr(clstr_file):
    """解析 CD-HIT .clstr 聚类文件"""
    clusters = defaultdict(list)
    cluster_id = None

    with open(clstr_file) as f:
        for line in f:
            if line.startswith(">Cluster"):
                cluster_id = int(line.split()[-1])
            else:
                # 格式: 0	123aa, >seq_id... *
                parts = line.split()
                seq_id = parts[2].rstrip(",").lstrip(">")
                is_representative = "*" in line
                clusters[cluster_id].append({
                    "seq_id": seq_id,
                    "is_representative": is_representative
                })

    return clusters

# 统计聚类
clusters = parse_cdhit_clstr("output.clstr")

# 提取代表序列
representatives = [
    next(s["seq_id"] for s in c if s["is_representative"])
    for c in clusters.values()
]

# 统计报告
print(f"原始序列数（估算）: {sum(len(c) for c in clusters.values())}")
print(f"去重后序列数（代表元）: {len(representatives)}")
print(f"去重率: {(1 - len(representatives)/sum(len(c) for c in clusters.values())) * 100:.1f}%")
print(f"最大聚类大小: {max(len(c) for c in clusters.values())}")
print(f"平均聚类大小: {sum(len(c) for c in clusters.values()) / len(clusters):.1f}")

# 保存代表序列
with open("output_representative.fasta") as fin, open("representatives.fasta", "w") as fout:
    for line in fin:
        if line.startswith(">"):
            seq_id = line[1:].split()[0]
            if seq_id in representatives:
                fout.write(line)
        else:
            fout.write(line)
```

### 5. 批量去重处理

**处理多个宏基因组样本：**

```bash
#!/bin/bash
# batch_dedup.sh

INPUT_DIR="./metagenomic_samples"
OUTPUT_DIR="./deduplicated"

mkdir -p "$OUTPUT_DIR"

for fasta_file in "$INPUT_DIR"/*.fasta; do
    filename=$(basename "$fasta_file" .fasta)
    echo "Processing $filename..."

    cd-hit -i "$fasta_file" \
           -o "$OUTPUT_DIR/${filename}_dedup" \
           -c 0.95 \
           -n 10 \
           -M 16000 \
           -T 8

    # 生成统计报告
    echo "Cluster stats for $filename:" >> "$OUTPUT_DIR/dedup_stats.txt"
    grep "^>" "$OUTPUT_DIR/${filename}_dedup.clstr" | wc -l >> "$OUTPUT_DIR/dedup_stats.txt"
done
```

### 6. 从聚类结果提取代表序列

```python
def extract_representatives(fasta_file, clstr_file, output_file):
    """从聚类结果提取代表序列（标记为 * 的序列）"""

    # 1. 解析代表序列 ID
    representatives = set()
    with open(clstr_file) as f:
        for line in f:
            if "*" in line:  # * 标记代表序列
                seq_id = line.split(">")[1].split()[0]
                representatives.add(seq_id)

    # 2. 从 FASTA 提取代表序列
    from Bio import SeqIO
    records = []
    for record in SeqIO.parse(fasta_file, "fasta"):
        if record.id in representatives:
            records.append(record)

    # 3. 写入输出
    SeqIO.write(records, output_file, "fasta")
    print(f"Extracted {len(records)} representative sequences to {output_file}")

# 使用
extract_representatives("output.fasta", "output.clstr", "representatives.fasta")
```

## Parameters Reference

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-i` | str | - | 输入 FASTA 文件路径 |
| `-o` | str | - | 输出文件前缀 |
| `-c` | float | 0.9 | 相似度阈值（0~1） |
| `-n` | int | 5 (蛋白) / 10 (核酸) | 字长 |
| `-M` | int | 400 | 最大内存 (MB) |
| `-T` | int | 1 | 线程数 |
| `-d` | int | 20 | 序列描述长度（0=无限） |
| `-g` | int | 0 (cd-hit-est) | 同义性（0 严格，1 宽松） |
| `-aS` | float | 0.0 | 目标序列覆盖率 |
| `-aL` | float | 0.0 | 查询序列覆盖率 |

## Common Pitfalls

- **字长选择错误**：核酸用 10，蛋白用 5；错误的字长导致聚类不准确
- **相似度阈值过高**：0.99+ 时几乎无去重效果；通常 0.95~0.97 平衡精度和去冗余
- **显存不足**：超大数据集时用 2D 聚类或增大 `-M`；或按样本批次处理
- **序列描述丢失**：默认截断描述行，用 `-d 0` 保留完整描述
- **聚类结果解析错误**：`.clstr` 文件格式复杂，用现有解析脚本或转换工具

## References

- [CD-HIT GitHub](https://github.com/weizhongli-lab/cdhit)
- [CD-HIT 用户指南](http://weizhongli-lab.org/cd-hit/)
- [论文：Huang et al. 2010](https://academic.oup.com/bioinformatics/article/26/5/680/205840)
