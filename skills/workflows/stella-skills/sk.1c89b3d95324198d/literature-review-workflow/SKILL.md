---
name: literature-review-workflow
description: 系统性文献综述工作流技能：搜索 PubMed/Semantic Scholar/bioRxiv， 筛选相关文献，提取关键信息，合成研究现状，生成结构化综述报告。 适用于快速文献调研和系统性综述准备。
license: Apache-2.0
metadata:
  skill-author: STELLA Team
  domain: scientific-literature
  version: 1.0.0
  status: active
routing:
  keywords:
  - literature review
  - PubMed search
  - systematic review
  - paper summary
  - research synthesis
  - bioRxiv
  - scientific literature
  tags:
  - scientific-literature
  - research
tools:
- search_pubmed
- search_semantic_scholar
- fetch_paper_abstract
- summarize_papers
- extract_key_findings
entry_conditions:
- 用户提供了研究主题或关键词
- 用户需要了解某领域的研究现状
output_expectations:
- 相关文献列表（标题、作者、年份、PMID）
- 每篇文献的关键发现摘要
- 综合性研究现状报告
procedure:
- step_id: literature_search
  title: 文献搜索
  goal: 从多个数据库检索相关文献
  biological_rationale: 多数据库联合搜索提高文献覆盖度
  instruction: |
    并行搜索 PubMed 和 Semantic Scholar。
    使用 MeSH 术语和自由词组合。
    时间范围：近 5 年（除非需要历史综述）。
    目标：获取 50-200 篇相关文献。
  recommended_tools:
  - search_pubmed
  - search_semantic_scholar
  required: true
- step_id: relevance_screening
  title: 相关性筛选
  goal: 基于摘要过滤最相关的文献
  biological_rationale: 聚焦于最相关文献提高综述质量
  instruction: |
    使用 fetch_paper_abstract 获取摘要并评估相关性。
    保留相关性评分 > 0.7 的文献。
    优先保留：引用次数多的文献、近 2 年的文献、高质量期刊的文献。
  recommended_tools:
  - fetch_paper_abstract
  required: true
- step_id: key_finding_extraction
  title: 关键发现提取
  goal: 从筛选文献中提取核心数据和结论
  biological_rationale: 结构化提取便于比较和综合分析
  instruction: |
    使用 extract_key_findings 提取：研究问题、方法、主要发现、局限性。
    重点关注：实验数据、统计显著性、临床相关性。
  recommended_tools:
  - extract_key_findings
  - summarize_papers
  required: true
- step_id: synthesis
  title: 综合报告生成
  goal: 整合所有文献信息生成结构化综述
  biological_rationale: 系统性综合揭示研究趋势和知识缺口
  instruction: |
    生成包含以下部分的综述报告：
    1. 研究现状概述
    2. 主要方法学进展
    3. 关键争议点
    4. 知识缺口和未来方向
    5. 参考文献列表（APA/Vancouver 格式）
  recommended_tools:
  - summarize_papers
  required: true
---

# Literature Review Workflow

## Overview

Automated systematic literature review workflow integrating multi-database search,
relevance screening, key finding extraction, and narrative synthesis. Produces
structured research summaries and identifies knowledge gaps.

## When to Use This Skill

- Rapidly understanding the state of a research field
- Preparing background sections for grant applications or papers
- Identifying contradictions or consensus in literature
- Finding methodological approaches used in similar studies

## Quick Start

```python
from bioagent.tools import search_pubmed, summarize_papers

papers = search_pubmed("CRISPR base editing therapeutic", max_results=50, years=3)
summary = summarize_papers(papers, focus="clinical applications")
```
