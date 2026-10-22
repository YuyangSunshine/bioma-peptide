---
name: progen2-generation
description: 生成式蛋白质语言模型 ProGen2：从 HuggingFace 加载预训练 ProGen2 模型，进行因果语言建模微调， 生成新肽段序列，支持 LoRA 低秩适配微调。用于抗菌肽、GFP 等肽段蛋白序列生成和优化。
license: MIT
metadata:
  skill-author: BioAgent Community
  domain: proteomics
  version: 1.0.0
  status: active
routing:
  keywords:
  - ProGen2
  - protein generation
  - sequence generation
  - causal language model
  - antimicrobial peptides
  - GFP
  - peptide design
  tags:
  - generation
  - language-model
  - protein-design
tools: []
entry_conditions:
- 用户提供了肽段序列数据（FASTA 格式）
- 用户想要生成新肽段或优化现有肽段
- 用户想要微调 ProGen2 模型
output_expectations:
- 生成的肽段序列列表（FASTA 格式）
- 微调后的模型检查点
- 生成序列的评分（pseudo-perplexity, 置信度等）
---

# ProGen2 Protein Generation

## Overview

ProGen2 是由 Salesforce Research 开发的 764M 参数因果语言模型，在 2.8 亿蛋白序列上预训练。
它通过自回归生成方式产生新的肽段和蛋白序列，可用于抗菌肽 (AMP) 发现、蛋白质设计和序列优化。

此技能提供：
1. **模型加载**：从 HuggingFace Hub 直接加载预训练 ProGen2
2. **序列生成**：采样生成新序列（支持 top-k、top-p、温度控制）
3. **微调训练**：使用 HuggingFace Trainer 进行全参数或 LoRA 微调
4. **条件生成**：从已知序列前缀生成延伸序列
5. **评分**：计算序列的伪困惑度 (pseudo-perplexity) 和对数似然

## When to Use This Skill

- **AMP 发现**：从宏基因组序列中发现新型抗菌肽候选
- **蛋白质设计**：优化蛋白质的稳定性、表达水平或功能性
- **序列评估**：计算未知序列的"合理性"评分（PLM embedding）
- **多样性生成**：为实验设计生成肽段库
- **跨物种泛化**：利用预训练模型做零样本肽段评估

**不适用场景：**
- 需要快速逆向折叠的场景（用 ProteinMPNN 或 OmegaFold）
- 需要确定性结构预测的场景（用 AlphaFold）
- 仅需序列相似性搜索的场景（用 BLAST 或 MMseqs2）

## Quick Start

```python
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import get_peft_model, LoraConfig, TaskType

# 1. 加载模型和分词器
model_name = "huggingface/progen2-base"
tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    model_name,
    torch_dtype=torch.float16,
    device_map="auto",
    trust_remote_code=True
)

# 2. 生成新序列（采样）
prompt = "MKVL"  # 肽段前缀
input_ids = tokenizer(prompt, return_tensors="pt")["input_ids"].to(model.device)
outputs = model.generate(
    input_ids,
    max_length=100,
    do_sample=True,
    top_p=0.95,
    temperature=1.0,
    num_return_sequences=5
)
sequences = tokenizer.batch_decode(outputs, skip_special_tokens=True)
print(sequences)

# 3. 评分序列（对数似然）
sequence = "MKVLFLKTKRKVL"
input_ids = tokenizer(sequence, return_tensors="pt")["input_ids"].to(model.device)
with torch.no_grad():
    logits = model(input_ids).logits
    # 计算伪困惑度（perplexity）
    log_likelihood = -torch.nn.functional.cross_entropy(
        logits[0, :-1].t(), input_ids[0, 1:], reduction='mean'
    )
print(f"Pseudo-perplexity: {log_likelihood.item():.4f}")
```

## Core Capabilities

### 1. 模型加载与初始化

**从 HuggingFace Hub 加载预训练 ProGen2：**

```python
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

model_name = "huggingface/progen2-base"  # 764M 参数
tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)

# GPU 自动分配
model = AutoModelForCausalLM.from_pretrained(
    model_name,
    torch_dtype=torch.float16,      # 半精度（FP16）节省显存
    device_map="auto",              # 自动 GPU 分配
    trust_remote_code=True
)
model.eval()
```

**支持的模型变体：**
- `huggingface/progen2-base` (764M)：最常用，AMP/蛋白设计
- `huggingface/progen2-xlarge` (1.3B)：更强表示能力

### 2. 无条件生成（随机采样）

从头开始生成新序列：

```python
def generate_sequences(model, tokenizer, num_sequences=10, max_length=100, temperature=1.0):
    """生成无条件序列"""
    outputs = model.generate(
        input_ids=None,
        max_length=max_length,
        do_sample=True,
        top_p=0.95,
        temperature=temperature,
        num_return_sequences=num_sequences,
        pad_token_id=tokenizer.eos_token_id
    )
    sequences = tokenizer.batch_decode(outputs, skip_special_tokens=True)
    return sequences

# 生成 10 个新肽段
new_sequences = generate_sequences(model, tokenizer, num_sequences=10, max_length=50)
for seq in new_sequences:
    print(seq)
```

**参数说明：**
- `temperature`：取值 0.5~2.0。低值更保守，高值更多样
- `top_p`：核采样概率阈值，通常 0.9~0.95
- `top_k`：从前 k 个概率最高的标记中采样

### 3. 条件生成（前缀扩展）

从已知肽段前缀生成延伸序列：

```python
def conditional_generate(model, tokenizer, prefix, num_generate=10, max_new_tokens=50):
    """从前缀生成序列"""
    input_ids = tokenizer(prefix, return_tensors="pt")["input_ids"].to(model.device)

    outputs = model.generate(
        input_ids,
        max_new_tokens=max_new_tokens,
        do_sample=True,
        top_p=0.95,
        temperature=0.8,
        num_return_sequences=num_generate,
        pad_token_id=tokenizer.eos_token_id
    )

    sequences = tokenizer.batch_decode(outputs, skip_special_tokens=True)
    return sequences

# 示例：从 AMP 前缀生成变体
amp_prefix = "GLFDIVKKV"  # 已知 AMP 序列
variants = conditional_generate(model, tokenizer, amp_prefix, num_generate=5)
for var in variants:
    print(var)
```

### 4. 序列评分（伪困惑度）

评估序列的"合理性"：

```python
def score_sequences(model, tokenizer, sequences):
    """计算伪困惑度（pseudo-perplexity）"""
    import torch
    import numpy as np

    scores = []
    with torch.no_grad():
        for seq in sequences:
            input_ids = tokenizer(seq, return_tensors="pt")["input_ids"].to(model.device)
            logits = model(input_ids).logits

            # 计算平均负对数似然
            log_likelihood = -torch.nn.functional.cross_entropy(
                logits[0, :-1].t(),
                input_ids[0, 1:],
                reduction='mean'
            )
            scores.append(log_likelihood.item())

    return np.array(scores)

# 评分候选序列
candidates = ["GLFDIVKKV", "MKVLFLKTKRKVL", "XXXXXXXXXXX"]
scores = score_sequences(model, tokenizer, candidates)
for seq, score in zip(candidates, scores):
    print(f"{seq}: {score:.4f}")
```

### 5. 微调训练（使用 HuggingFace Trainer）

使用自己的肽段数据集进行微调：

```python
from transformers import Trainer, TrainingArguments
from datasets import Dataset
import pandas as pd

# 1. 准备数据集
sequences_df = pd.read_csv("amp_sequences.csv")  # 含 "sequence" 列
dataset = Dataset.from_pandas(sequences_df)

# 2. 分词
def tokenize_function(examples):
    return tokenizer(examples["sequence"], max_length=512, truncation=True)

tokenized_dataset = dataset.map(tokenize_function, batched=True)
train_test_split = tokenized_dataset.train_test_split(test_size=0.1)

# 3. 定义训练参数
training_args = TrainingArguments(
    output_dir="./progen2-amp-finetuned",
    overwrite_output_dir=True,
    num_train_epochs=3,
    per_device_train_batch_size=4,
    per_device_eval_batch_size=4,
    learning_rate=2e-5,
    weight_decay=0.01,
    evaluation_strategy="epoch",
    save_strategy="epoch",
    logging_steps=100,
    fp16=True,
    remove_unused_columns=False,
)

# 4. 微调
trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_test_split["train"],
    eval_dataset=train_test_split["test"],
)

trainer.train()
```

### 6. LoRA 低秩适配微调

用更少显存微调大模型：

```python
from peft import get_peft_model, LoraConfig, TaskType

# LoRA 配置
lora_config = LoraConfig(
    task_type=TaskType.CAUSAL_LM,
    r=16,                           # LoRA 秩
    lora_alpha=32,                  # 缩放因子
    lora_dropout=0.05,
    bias="none",
    target_modules=["q_proj", "v_proj"],  # 作用于 Q/V 投影层
)

# 应用 LoRA
model = get_peft_model(model, lora_config)
model.print_trainable_parameters()  # 打印可训练参数数量

# 后续与标准微调相同...
trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_test_split["train"],
    eval_dataset=train_test_split["test"],
)
trainer.train()
```

## Parameters Reference

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_name` | str | `huggingface/progen2-base` | HuggingFace Hub 模型名称 |
| `max_length` | int | 100 | 生成的最大序列长度 |
| `temperature` | float | 1.0 | 采样温度（0.5~2.0） |
| `top_p` | float | 0.95 | 核采样概率阈值 |
| `top_k` | int | 50 | 从前 k 个标记中采样 |
| `num_return_sequences` | int | 1 | 返回的序列数 |
| `learning_rate` | float | 2e-5 | 微调学习率 |
| `lora_r` | int | 16 | LoRA 秩 |
| `lora_alpha` | int | 32 | LoRA 缩放因子 |

## Common Pitfalls

- **显存溢出**：用 `torch.float16` 和 LoRA 减少显存占用；或用 `device_map="auto"` 自动分配
- **生成质量差**：降低 `temperature`（更保守），或增加 `top_p`（更多样性）；微调时使用更多高质量序列
- **微调过拟合**：使用 LoRA 而非全参数微调；增加 `lora_dropout`；使用早停（early stopping）
- **特殊标记处理**：确保 `pad_token_id` 和 `eos_token_id` 设置正确
- **序列长度**：超过 512 的序列在分词时被截断，改大 `max_length` 或使用滑动窗口

## References

- [ProGen2 论文](https://arxiv.org/abs/2206.13517)
- [HuggingFace ProGen2 模型卡](https://huggingface.co/huggingface/progen2-base)
- [PEFT LoRA 文档](https://github.com/huggingface/peft)
- [HuggingFace Transformers 生成 API](https://huggingface.co/docs/transformers/generation_strategies)
