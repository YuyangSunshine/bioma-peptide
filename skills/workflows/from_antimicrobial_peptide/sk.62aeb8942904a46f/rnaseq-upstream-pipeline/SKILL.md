---
name: rnaseq-upstream-pipeline
description: 统一的上游 RNA-seq 流程：从原始测序数据（FASTQ）到基因表达矩阵的完整处理流程。 包括质量控制（FastQC）、序列剪切（fastp）、序列比对（STAR）、表达量定量（featureCounts）、 差异表达分析（DESeq2）。用于转录组学研究、抗菌肽诱导基因表达变化分析、宿主免疫应答监测。
license: MIT
metadata:
  skill-author: BioAgent Community
  domain: genomics
  version: 1.0.0
  status: active
routing:
  keywords:
  - RNA-seq
  - transcriptomics
  - fastq
  - alignment
  - quantification
  - differential expression
  - STAR
  - featureCounts
  - DESeq2
  tags:
  - preprocessing
  - alignment
  - quantification
  - transcriptomics
tools: []
entry_conditions:
- 用户有原始 RNA-seq 测序数据（FASTQ 格式）
- 需要进行质量控制和数据预处理
- 需要与参考基因组比对并定量表达
output_expectations:
- 质量控制报告（FastQC）
- 比对后的 BAM 文件
- 表达量矩阵（counts table）
- 差异表达基因列表（可选）
- 可视化结果（PCA, MA plot 等）
---

# Unified RNA-seq Upstream Pipeline

## Overview

此技能整合了 RNA-seq 数据分析的标准流程，从原始 FASTQ 文件到基因表达定量：

1. **质量评估（QC）**：FastQC 检查原始数据质量
2. **数据清理**：fastp 移除接头和低质量序列
3. **序列比对**：STAR 对标参考基因组快速比对
4. **表达定量**：featureCounts 统计每个基因的比对读数
5. **差异分析**：DESeq2 鉴定差异表达基因

整个流程可用 Snakemake/Nextflow 自动化，支持单端和配对末端数据。

## When to Use This Skill

- **抗菌肽诱导的转录组**：AMP 处理的细菌/真核细胞转录变化
- **宿主免疫应答**：感染后的免疫基因表达变化（GO/KEGG 分析）
- **时间序列 RNA-seq**：多个时间点的动态基因表达
- **组织/细胞类型比较**：不同样本间基因表达差异
- **质量过滤**：从百万级读数筛选高质量数据

## Quick Start

```bash
# 1. 安装工具
conda install -c bioconda fastqc fastp star samtools subread

# 2. 数据质量检查
fastqc sample1_R1.fastq.gz sample1_R2.fastq.gz -o qc_reports/

# 3. 数据清理（去接头、低质量）
fastp \
  -i sample1_R1.fastq.gz -I sample1_R2.fastq.gz \
  -o sample1_R1_clean.fastq.gz -O sample1_R2_clean.fastq.gz \
  -q 20 -l 50 -w 4

# 4. 构建 STAR 基因组索引（仅需一次）
STAR --runThreadN 8 \
  --runMode genomeGenerate \
  --genomeDir ./star_index \
  --genomeFastaFiles GCF_000005845.2_ASM584v2_genomic.fna \
  --gtfFile GCF_000005845.2_ASM584v2_genomic.gtf \
  --sjdbOverhang 100

# 5. 序列比对
STAR --genomeDir ./star_index \
  --readFilesIn sample1_R1_clean.fastq.gz sample1_R2_clean.fastq.gz \
  --readFilesCommand zcat \
  --outFileNamePrefix sample1_ \
  --outSAMtype BAM SortedByCoordinate \
  --runThreadN 8

# 6. 表达定量
featureCounts \
  -T 8 \
  -p -a genome.gtf \
  -o gene_counts.txt \
  sample1_Aligned.sortedByCoord.out.bam sample2_Aligned.sortedByCoord.out.bam

# 7. 差异表达分析（使用 R/DESeq2）
R << 'EOF'
library(DESeq2)
library(tidyverse)

# 读取计数矩阵
counts <- read.table("gene_counts.txt", header=T, row.names=1, skip=1)
counts <- counts[, -c(1:5)]  # 移除元数据列

# 定义实验设计
design <- data.frame(
  sample = colnames(counts),
  group = c("ctrl", "ctrl", "treated", "treated")
)

# DESeq2 分析
dds <- DESeqDataSetFromMatrix(
  countData = counts,
  colData = design,
  design = ~group
)

dds <- DESeq(dds)
res <- results(dds, contrast=c("group", "treated", "ctrl"))

# 输出差异基因
res_sig <- res[res$padj < 0.05 & abs(res$log2FoldChange) > 1, ]
write.csv(res_sig, "diff_genes.csv")

# 可视化
plotMA(res)
plotPCA(vst(dds))
EOF
```

## Core Capabilities

### 1. 质量控制（FastQC）

```bash
# 单样品
fastqc sample.fastq.gz -o qc_output/

# 批量处理
for fq in *.fastq.gz; do
  fastqc "$fq" -o qc_output/
done

# 生成 HTML 报告
# 检查项：Per base sequence quality, GC content, adapter contamination 等
```

**FastQC 解读：**
- **Per base sequence quality**：理想值 Q≥30（99.9% 准确性）
- **Per sequence GC content**：应与参考基因组相符
- **Adapter content**：如果检测到接头，需用 fastp 移除

### 2. 数据清理（fastp）

```bash
# 配对末端测序（PE）数据清理
fastp \
  -i read1.fastq.gz -I read2.fastq.gz \
  -o read1_clean.fastq.gz -O read2_clean.fastq.gz \
  -q 20 \              # 最小质量分数（Phred ≥20）
  -l 50 \              # 最小长度
  --adapter_sequence AGATCGGAAGAGC \  # Illumina 接头
  -w 4 \               # 线程数
  --json fastp.json    # 输出 JSON 报告

# 单端测序（SE）数据清理
fastp \
  -i read.fastq.gz \
  -o read_clean.fastq.gz \
  -q 20 -l 50

# 输出：清理后 FASTQ，JSON 报告包含移除的读数、GC 分布等
```

### 3. 序列比对（STAR）

**构建基因组索引（仅需一次）：**

```bash
# 下载参考基因组和注释
wget ftp://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.fna.gz
wget ftp://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.gtf.gz
gunzip *.gz

# 构建索引
STAR --runThreadN 8 \
  --runMode genomeGenerate \
  --genomeDir ./star_index \
  --genomeFastaFiles GCF_000005845.2_ASM584v2_genomic.fna \
  --gtfFile GCF_000005845.2_ASM584v2_genomic.gtf \
  --sjdbOverhang 100   # reads_length - 1
```

**执行比对：**

```bash
# 配对末端数据
STAR --genomeDir ./star_index \
  --readFilesIn sample_R1.fastq.gz sample_R2.fastq.gz \
  --readFilesCommand zcat \
  --outFileNamePrefix sample_ \
  --outSAMtype BAM SortedByCoordinate \
  --quantMode GeneCounts \      # 输出基因计数
  --runThreadN 8

# 单端数据
STAR --genomeDir ./star_index \
  --readFilesIn sample.fastq.gz \
  --readFilesCommand zcat \
  --outFileNamePrefix sample_ \
  --outSAMtype BAM SortedByCoordinate \
  --runThreadN 8
```

**输出文件：**
- `sample_Aligned.sortedByCoord.out.bam`：比对后的 BAM 文件
- `sample_Log.final.out`：比对统计信息
- `sample_ReadsPerGene.out.tab`：每基因读数计数（可选）

### 4. 表达定量（featureCounts）

```bash
# 单个样品
featureCounts \
  -T 8 \
  -p \                          # 配对末端模式
  -a genome.gtf \               # 注释文件
  -o sample_counts.txt \
  sample_Aligned.sortedByCoord.out.bam

# 多个样品（推荐）
featureCounts \
  -T 8 \
  -p \
  -a genome.gtf \
  -o gene_counts_matrix.txt \
  sample1.bam sample2.bam sample3.bam sample4.bam

# 输出说明
# gene_counts_matrix.txt 包含：
# - 列 1-5：基因坐标信息
# - 列 6+：各样品的计数值
```

### 5. 差异表达分析（DESeq2）

**R 脚本完整示例：**

```r
library(DESeq2)
library(tidyverse)
library(pheatmap)

# 读取 featureCounts 输出
counts_matrix <- read.table(
  "gene_counts_matrix.txt",
  header = TRUE,
  row.names = 1,
  skip = 1
)

# 移除元数据列（前 5 列）
counts <- counts_matrix[, -c(1:5)]

# 获取基因名（行名）
gene_names <- rownames(counts_matrix)

# 定义实验设计
sample_info <- data.frame(
  sample_id = colnames(counts),
  condition = c("control", "control", "treated", "treated")
)
rownames(sample_info) <- sample_info$sample_id

# 构建 DESeqDataSet
dds <- DESeqDataSetFromMatrix(
  countData = counts,
  colData = sample_info,
  design = ~condition
)

# 预过滤（移除低表达基因）
keep <- rowSums(counts(dds)) >= 10
dds <- dds[keep, ]

# DESeq2 标准分析流程
dds <- DESeq(dds)

# 获取结果
res <- results(dds, contrast = c("condition", "treated", "control"))

# 按 adjusted p-value 排序
res_ordered <- res[order(res$padj), ]

# 筛选显著基因（FDR < 0.05，|log2FC| > 1）
sig_genes <- res[!is.na(res$padj) & res$padj < 0.05 & abs(res$log2FoldChange) > 1, ]

# 输出结果
write.csv(as.data.frame(res_ordered), "deseq2_results_all.csv")
write.csv(as.data.frame(sig_genes), "deseq2_results_sig.csv")

# 可视化 1：MA-plot
plotMA(res, main = "MA-plot (DESeq2)")

# 可视化 2：Volcano plot
library(ggplot2)
res_df <- as.data.frame(res)
ggplot(res_df, aes(x = log2FoldChange, y = -log10(padj))) +
  geom_point(aes(color = padj < 0.05)) +
  theme_minimal() +
  labs(title = "Volcano Plot", x = "log2(FC)", y = "-log10(padj)")

# 可视化 3：PCA
vst_data <- vst(dds)
plotPCA(vst_data, intgroup = "condition")

# 可视化 4：热图
sig_counts <- counts(dds, normalized = TRUE)[rownames(sig_genes), ]
pheatmap(log2(sig_counts + 1), scale = "row", main = "Significant Genes")
```

### 6. 自动化流程 (Snakemake)

**Snakefile：**

```python
import os

SAMPLES = ["sample1", "sample2", "sample3", "sample4"]
REF_GENOME = "genome.fna"
GTF_FILE = "genome.gtf"
INDEX_DIR = "star_index"

rule all:
    input:
        "qc_report.html",
        "gene_counts_matrix.txt",
        "diff_genes.csv"

rule fastqc:
    input:
        "{sample}_R1.fastq.gz", "{sample}_R2.fastq.gz"
    output:
        "qc/{sample}_R1_fastqc.html", "qc/{sample}_R2_fastqc.html"
    shell:
        "fastqc -o qc {input}"

rule fastp:
    input:
        r1="{sample}_R1.fastq.gz", r2="{sample}_R2.fastq.gz"
    output:
        r1="clean/{sample}_R1_clean.fastq.gz", r2="clean/{sample}_R2_clean.fastq.gz"
    shell:
        "fastp -i {input.r1} -I {input.r2} -o {output.r1} -O {output.r2} -q 20 -l 50"

rule build_index:
    input:
        genome=REF_GENOME, gtf=GTF_FILE
    output:
        directory(INDEX_DIR)
    shell:
        "STAR --runThreadN 8 --runMode genomeGenerate "
        "--genomeDir {output} --genomeFastaFiles {input.genome} "
        "--gtfFile {input.gtf}"

rule star_align:
    input:
        r1="clean/{sample}_R1_clean.fastq.gz",
        r2="clean/{sample}_R2_clean.fastq.gz",
        index=directory(INDEX_DIR)
    output:
        bam="bam/{sample}_Aligned.sortedByCoord.out.bam"
    shell:
        "STAR --genomeDir {input.index} "
        "--readFilesIn {input.r1} {input.r2} --readFilesCommand zcat "
        "--outFileNamePrefix bam/{wildcards.sample}_ "
        "--outSAMtype BAM SortedByCoordinate --runThreadN 8"

rule feature_counts:
    input:
        bam=expand("bam/{sample}_Aligned.sortedByCoord.out.bam", sample=SAMPLES),
        gtf=GTF_FILE
    output:
        "gene_counts_matrix.txt"
    shell:
        "featureCounts -T 8 -p -a {input.gtf} -o {output} {input.bam}"
```

## Parameters Reference

| 工具 | 参数 | 说明 | 推荐值 |
|------|------|------|--------|
| fastp | -q | 最小质量分数 | 20 |
| fastp | -l | 最小长度 | 50 bp |
| STAR | sjdbOverhang | reads 长度 - 1 | 100 |
| featureCounts | -p | 配对末端模式 | 配对时使用 |
| DESeq2 | padj 阈值 | FDR 调整 p 值 | < 0.05 |
| DESeq2 | |log2FC| 阈值 | > 1 (通常) |

## Common Pitfalls

- **参考基因组版本不匹配**：确保 FASTA 和 GTF 来自同一版本
- **sjdbOverhang 设置错误**：应为 reads_length - 1；100 bp reads 用 99
- **比对率低**：可能原因：参考基因组错误、数据质量差、物种不对；检查 STAR Log.final.out
- **重复计数**：featureCounts 默认忽略多映射读；可用 `-M` 包含
- **样品混淆**：确保 FASTQ 文件顺序与 design matrix 一致

## References

- [STAR 文档](https://github.com/alexdobin/STAR)
- [featureCounts 文档](https://subread.sourceforge.net/featureCounts.html)
- [DESeq2 论文](https://genomebiology.biomedcentral.com/articles/10.1186/s13059-014-0550-8)
- [RNA-seq 最佳实践](https://rna-seqblog.com/rna-seq-analysis-using-star-and-deseq2/)
