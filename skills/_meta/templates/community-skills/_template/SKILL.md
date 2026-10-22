---
# ===== 基础字段（必填）=====
name: my-skill-name
description: |
  一句话描述：这个技能做什么，何时使用。
  要足够具体，让 Skill Router 能准确匹配。
  例如：分析 ChIP-seq 数据，识别转录因子结合位点，生成峰值注释报告。
license: MIT
metadata:
  skill-author: "你的姓名"
  domain: genomics             # proteomics | genomics | cheminformatics | transcriptomics | ...
  version: "1.0.0"
  status: active               # active | experimental | deprecated

# ===== 以下字段均为可选，按需填写 =====

routing:                       # 增强路由匹配精度
  keywords:
    - keyword1
    - keyword2
    - keyword3
  tags:
    - tag1

tools:                         # 绑定可调用工具函数名（需在 tools/ 目录实现）
  - my_tool_function_1
  # - my_tool_function_2

entry_conditions:              # 使用此技能的前提条件
  - "用户提供了..."

output_expectations:           # 预期输出
  - "输出 1"

procedure:                     # 多步工作流（可选）
  - step_id: step_1
    title: "步骤 1"
    goal: "此步骤的具体目标"
    biological_rationale: "为什么需要这一步（生物学原理）"
    instruction: |
      详细指令，描述 LLM 应如何执行此步骤。
      包括参数选择、阈值、注意事项等。
    recommended_tools:
      - my_tool_function_1
    required: true
---

# My Skill Name

## Overview

这个技能的整体介绍（2-3 段）。说明：
1. 这个技能解决什么科学问题
2. 基于什么方法/工具
3. 与其他技能的区别

## When to Use This Skill

- 场景 1：描述何时适合使用此技能
- 场景 2
- 场景 3

**不适用场景：**
- 反例 1（应使用其他技能的情况）

## Quick Start

```python
# 基本使用示例
from bioagent.tools import my_tool_function_1

result = my_tool_function_1(param1="value1")
print(result)
```

## Core Capabilities

### 1. 功能 A
详细说明、代码示例、参数含义。

### 2. 功能 B
详细说明。

## Parameters Reference

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| param1 | str | 必填 | 参数说明 |
| param2 | int | 100 | 参数说明 |

## Common Pitfalls

- 易错点 1：如何避免
- 易错点 2：如何处理

## References

- [相关论文或文档链接]
- `references/` 目录下有更详细的 API 文档
