---
name: mmseqs2-search
description: MMseqs2 序列相似性搜索与聚类：超快速的蛋白/核酸序列搜索引擎，比 BLAST 快 1000 倍。 支持本地数据库搜索、远程搜索、聚类、多序列比对、E-value 统计。用于宏基因组序列注释、 同源蛋白识别、AMP 库搜索等高通量任务。
license: MIT
metadata:
  skill-author: BioAgent Community
  domain: genomics
  version: 1.0.0
  status: active
routing:
  keywords:
  - MMseqs2
  - sequence search
  - similarity search
  - clustering
  - homology
  - fast alignment
  - metagenomics
  tags:
  - search
  - alignment
  - clustering
tools:
- sciagent_run_mmseqs2
entry_conditions:
- 用户需要快速大规模序列搜索
- 有查询序列和数据库序列
- 需要按 E-value 或相似度阈值筛选结果
output_expectations:
- 搜索结果表（query, target, identity, e-value, score）
- 序列对齐（可选）
- 聚类文件（可选）
---

# MMseqs2: Ultra-Fast Sequence Search and Clustering

## Overview

MMseqs2 (Many-vs-Many Sequence Searching) 是由 Max Planck Institute 开发的超高速序列搜索工具。
通过 k-mer 索引和聚类预处理，实现比 BLAST 快 1000+ 倍的搜索速度，同时保持高敏感性。

此技能提供：
1. **本地数据库搜索**：快速搜索海量序列库
2. **相似度聚类**：按相似度将序列分组
3. **多对多搜索**：找到所有相似序列对
4. **E-value 统计**：可靠的统计显著性评估
5. **双向搜索**：发现互惠最佳匹配（RBH）

查看本 Skill 后，调用 `sciagent_run_mmseqs2` 执行 FASTA 搜索或聚类。若任务需要 PSI-BLAST、HMMER 或严格的互惠最佳匹配流程，改用 `bio-sequence-similarity` Skill 中暴露的工具。

## When to Use This Skill

- **宏基因组注释**：百万级 ORF 快速注释到已知数据库
- **AMP 库搜索**：在 APD/CAMP 数据库中快速查找相似 AMP
- **蛋白质聚类**：大规模蛋白质数据去冗余和分类
- **直系同源识别**：多物种间查找同源蛋白（RBH）
- **序列过滤**：快速移除已知序列，发现新序列

**vs BLAST：**速度快 100~1000 倍，敏感性相当；**vs CD-HIT：**更精确的相似度评估，支持 E-value 统计

## Quick Start

```bash
# 1. 安装 MMseqs2
conda install -c bioconda mmseqs2

# 2. 构建查询数据库
mmseqs createdb query.fasta queryDB

# 3. 构建目标数据库
mmseqs createdb target.fasta targetDB

# 4. 搜索
mmseqs search queryDB targetDB result.m8 tmp --min-seq-id 0.5 -e 1e-3

# 5. 转换输出格式
mmseqs convertalis queryDB targetDB result.m8 result.tsv

# 6. 查看结果
head result.tsv
```

## Core Capabilities

### 1. 基本序列搜索

**快速本地搜索：**

```bash
# 1. 创建数据库
mmseqs createdb sequences.fasta seqDB

# 2. 执行搜索（E-value < 1e-3，≥50% 相似度）
mmseqs search queryDB seqDB resultDB tmp \
  --min-seq-id 0.5 \
  -e 1e-3 \
  --threads 8 \
  -v 3

# 3. 转换为 BLAST M8 格式
mmseqs convertalis queryDB seqDB resultDB result.m8

# 4. 或转换为 TSV
mmseqs convertalis queryDB seqDB resultDB result.tsv --format-mode 2

# M8 格式列：query, subject, identity%, aln_len, mismatch, gap_open,
#           query_start, query_end, subject_start, subject_end, evalue, bitscore
```

**参数说明：**
- `-e`：E-value 阈值（1e-3 较严格）
- `--min-seq-id`：最小相似度（0.5=50%）
- `--sens`：敏感性（1~7），高值更敏感但慢
- `-v`：详细程度（0~3）

### 2. 宏基因组 ORF 搜索

```bash
# 完整工作流：去冗余 + 搜索 + 注释

# Step 1: 聚类移除冗余 ORF
mmseqs cluster orfDB orfDB_clustered tmp \
  --min-seq-id 0.9 \
  -c 0.8 \
  --cov-mode 1

# Step 2: 提取代表序列
mmseqs createseqfiledb orfDB orfDB_clustered orfDB_rep

# Step 3: 搜索已知 AMP 库
mmseqs search orfDB_rep known_amp_db result tmp \
  --min-seq-id 0.5 \
  -e 1e-5

# Step 4: 转换结果
mmseqs convertalis orfDB_rep known_amp_db result result.tsv --format-mode 2
```

### 3. 聚类分析

```bash
# 按 95% 相似度聚类
mmseqs cluster seqDB seqDB_clustered tmp \
  --min-seq-id 0.95 \
  -c 0.8 \
  --cov-mode 1 \
  --threads 8

# 创建聚类代表序列集合
mmseqs createseqfiledb seqDB seqDB_clustered seqDB_rep

# 查看聚类成员
mmseqs createtsv seqDB seqDB seqDB_clustered clusters.tsv
cat clusters.tsv | head
# 格式：representative_id    cluster_member_id
```

### 4. 双向搜索 (RBH - Reciprocal Best Hit)

```bash
# 在两个物种间找直系同源蛋白
# species_A ←→ species_B

# Step 1: A → B
mmseqs search speciesA_db speciesB_db resultAB tmp -e 1e-5

# Step 2: B → A
mmseqs search speciesB_db speciesA_db resultBA tmp -e 1e-5

# Step 3: 找互惠最佳匹配
mmseqs filterdb resultAB resultAB_best -e 1e-5 --max-accepted 1 --max-rejected 0

mmseqs filterdb resultBA resultBA_best -e 1e-5 --max-accepted 1 --max-rejected 0

# Step 4: 取交集
mmseqs mergedbs speciesA_db speciesA_db resultAB_best resultBA_best resultRBH

mmseqs convertalis speciesA_db speciesB_db resultRBH rbh.tsv --format-mode 2
```

### 5. Python 脚本搜索和结果解析

```python
import subprocess
import pandas as pd

def run_mmseqs_search(query_file, target_file, evalue=1e-3, min_id=0.5):
    """运行 MMseqs2 搜索"""

    # 创建数据库
    subprocess.run(["mmseqs", "createdb", query_file, "queryDB"])
    subprocess.run(["mmseqs", "createdb", target_file, "targetDB"])

    # 搜索
    subprocess.run([
        "mmseqs", "search",
        "queryDB", "targetDB", "resultDB", "tmp",
        "--min-seq-id", str(min_id),
        "-e", str(evalue),
        "--threads", "8"
    ])

    # 转换输出
    subprocess.run([
        "mmseqs", "convertalis",
        "queryDB", "targetDB", "resultDB", "result.m8"
    ])

    # 读取结果
    df = pd.read_csv(
        "result.m8",
        sep="\t",
        names=["query", "target", "identity", "aln_len", "mismatch", "gap_open",
               "query_start", "query_end", "target_start", "target_end", "evalue", "bitscore"]
    )

    return df

# 使用
results = run_mmseqs_search("query.fasta", "target.fasta", evalue=1e-3)

# 统计
print(f"Total hits: {len(results)}")
print(f"Queries with hits: {results['query'].nunique()}")
print(f"Average identity: {results['identity'].mean():.2f}%")

# 过滤
high_conf = results[results['identity'] >= 90]
print(f"High-confidence hits (≥90% identity): {len(high_conf)}")

# 按查询分组，取最佳匹配
best_hits = results.loc[results.groupby('query')['bitscore'].idxmax()]
print(f"Best hit per query: {len(best_hits)}")
```

### 6. 远程数据库搜索

```bash
# 对 UniProt/EggNOG 等预构建数据库搜索（免客户端）
mmseqs easy-search query.fasta UniProtKB/Swiss-Prot result.m8 tmp \
  --threads 8 \
  -e 1e-5

# 可用预构建数据库：
# UniProtKB/Swiss-Prot, UniProtKB/TrEMBL, PDB, Pfam, ...
```

## Parameters Reference

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `-e` | float | 0.001 | E-value 阈值 |
| `--min-seq-id` | float | 0.0 | 最小序列相似度 |
| `-c` | float | 0.8 | 覆盖度阈值 |
| `--cov-mode` | int | 0 | 覆盖度模式（0~5） |
| `--sens` | float | 5.7 | 敏感性（1~7） |
| `--threads` | int | 1 | 线程数 |
| `-v` | int | 3 | 详细程度 |
| `--max-accepted` | int | - | 保留的最大结果数 |

## Common Pitfalls

- **E-value 过宽松**：e.g., -e 1 时无法过滤真阳性；通常 1e-3~1e-5
- **相似度阈值混淆**：`--min-seq-id 0.5` 是 50% 相似度（共 50% 相同），不是 50% 覆盖
- **数据库格式错误**：必须用 `createdb` 创建二进制数据库，不能直接用 FASTA
- **内存溢出**：超大搜索用 `--split 2` 参数分块处理
- **结果解释错误**：identity% 是相同碱基/氨基酸的比例，不是相似度评分

## References

- [MMseqs2 GitHub](https://github.com/soedinglab/mmseqs2)
- [MMseqs2 Wiki 文档](https://github.com/soedinglab/mmseqs2/wiki)
- [论文：Steinegger & Söding 2017](https://www.nature.com/articles/nbt.3988)
