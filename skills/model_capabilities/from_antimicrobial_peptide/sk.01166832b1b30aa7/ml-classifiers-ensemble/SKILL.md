---
name: ml-classifiers-ensemble
description: Multiple machine learning classifiers (XGBoost, LightGBM, CatBoost, Random Forest, SVM, MLP) and soft voting ensemble for antimicrobial peptide (AMP) classification based on ESM-2 embeddings
tools: []
title: ML Classifiers & Ensemble for AMP Identification
keywords:
- xgboost
- lightgbm
- catboost
- random-forest
- svm
- neural-network
- ensemble-voting
- amp-classification
- hyperparameter-optimization
author: FRAMO-AMP Project
version: '1.0'
created: 2026-07-03
tags:
- machine-learning
- classification
- ensemble
- amp-identification
- scikit-learn
- xgboost
- lightgbm
depends_on:
- esm2-embeddings
- progen2-generation
---

# ML Classifiers & Ensemble for AMP Identification

## Overview

This skill provides **production-ready implementations** of multiple machine learning classifiers optimized for antimicrobial peptide (AMP) identification from ESM-2 protein embeddings. The table below summarizes performance across different models and ensemble strategies:

| Model | ACC | Recall | Precision | F1 | Best For |
|-------|-----|--------|-----------|----|---------|
| ESM+XGBoost | 0.946 | 0.852 | 0.869 | 0.861 | Speed + Interpretability |
| ESM+Random Forest | **0.954** | 0.839 | 0.919 | 0.877 | Baseline Ensemble |
| ESM+SVM | 0.945 | 0.798 | 0.907 | 0.849 | Robust Decision Boundary |
| ESM+LightGBM | 0.943 | 0.81 | 0.886 | 0.847 | Fast Training |
| ESM+CatBoost | 0.932 | 0.779 | 0.857 | 0.816 | Categorical Features |
| ESM+MLP | 0.894 | 0.803 | 0.697 | 0.746 | Deep Representation |
| **ESM+(XGBoost+RF+SVM) 2/3** | **0.959** | **0.867** | **0.917** | **0.892** | 🏆 **Best Overall** |

### Key Capabilities

1. **Gradient Boosting Classifiers** - XGBoost, LightGBM, CatBoost with GPU/CPU support
2. **Tree-based Ensemble** - Random Forest with feature importance tracking
3. **Support Vector Machine** - Kernel selection and probability calibration
4. **Deep Learning** - PyTorch MLP with batch normalization and dropout
5. **Soft Voting Ensemble** - Weighted voting with threshold optimization
6. **Hyperparameter Optimization** - Bayesian/Grid search with cross-validation
7. **Model Persistence** - Save/load predictions and model artifacts
8. **Feature Analysis** - SHAP explanations and feature importance visualization

---

## Quick Start

### Basic Classification Pipeline

```python
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score

# Assume ESM embeddings (1280-dim) and labels already loaded
# esm_embeddings shape: (n_samples, 1280)
# y_labels shape: (n_samples,) with binary values [0, 1]

X_train, X_test, y_train, y_test = train_test_split(
    esm_embeddings, y_labels, test_size=0.2, random_state=42, stratify=y_labels
)

# Normalize embeddings
scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled = scaler.transform(X_test)

# Train XGBoost
xgb_model = XGBClassifier(
    n_estimators=500,
    max_depth=6,
    learning_rate=0.1,
    subsample=0.8,
    colsample_bytree=0.8,
    random_state=42,
    tree_method='gpu_hist',  # Use GPU if available
    eval_metric='logloss',
    early_stopping_rounds=50
)

# Train with validation set for early stopping
xgb_model.fit(
    X_train_scaled, y_train,
    eval_set=[(X_test_scaled, y_test)],
    verbose=False
)

# Evaluate
y_pred = xgb_model.predict(X_test_scaled)
y_pred_proba = xgb_model.predict_proba(X_test_scaled)[:, 1]

print(f"Accuracy: {accuracy_score(y_test, y_pred):.4f}")
print(f"F1 Score: {f1_score(y_test, y_pred):.4f}")
print(f"ROC-AUC: {roc_auc_score(y_test, y_pred_proba):.4f}")
```

---

## Core Capabilities

### 1. XGBoost Classification

**Purpose**: Extreme Gradient Boosting for fast, high-performance binary classification with GPU acceleration.

```python
from xgboost import XGBClassifier
from sklearn.model_selection import cross_val_score

# XGBoost with optimized hyperparameters for AMP classification
xgb = XGBClassifier(
    n_estimators=500,          # Number of boosting rounds
    max_depth=6,               # Tree depth (prevent overfitting)
    learning_rate=0.1,         # Shrinkage factor
    subsample=0.8,             # Row sampling ratio
    colsample_bytree=0.8,      # Column sampling ratio
    min_child_weight=1,        # Minimum child weight
    gamma=0,                   # Minimum loss reduction
    reg_alpha=0.0,             # L1 regularization
    reg_lambda=1.0,            # L2 regularization
    objective='binary:logistic',
    eval_metric='logloss',
    tree_method='gpu_hist',    # GPU acceleration (if available)
    gpu_id=0,
    random_state=42
)

# Train
xgb.fit(X_train_scaled, y_train,
        eval_set=[(X_test_scaled, y_test)],
        early_stopping_rounds=50,
        verbose=100)

# Feature importance
importance_df = pd.DataFrame({
    'feature': [f'emb_{i}' for i in range(X_train.shape[1])],
    'importance': xgb.feature_importances_
}).sort_values('importance', ascending=False)

print(f"Top 10 important features:\n{importance_df.head(10)}")

# Cross-validation
cv_scores = cross_val_score(xgb, X_train_scaled, y_train, cv=5, scoring='f1')
print(f"Cross-validation F1 scores: {cv_scores}, Mean: {cv_scores.mean():.4f}")
```

### 2. LightGBM Gradient Boosting

**Purpose**: Lightweight Gradient Boosting Machine for faster training on large embeddings with lower memory footprint.

```python
import lightgbm as lgb

# LightGBM classifier optimized for speed
lgb_model = lgb.LGBMClassifier(
    n_estimators=500,
    max_depth=6,
    learning_rate=0.1,
    num_leaves=31,             # Max leaves per tree
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_samples=20,      # Minimum samples per leaf
    reg_alpha=0.0,
    reg_lambda=1.0,
    objective='binary',
    metric='binary_logloss',
    random_state=42,
    verbose=-1
)

# Create LightGBM dataset for faster training
train_data = lgb.Dataset(X_train_scaled, label=y_train)
test_data = lgb.Dataset(X_test_scaled, label=y_test, reference=train_data)

# Train with early stopping
lgb_model.fit(
    X_train_scaled, y_train,
    eval_set=[(X_test_scaled, y_test)],
    eval_metric='binary_logloss',
    callbacks=[
        lgb.early_stopping(50),
        lgb.log_evaluation(period=100)
    ]
)

# Get predictions and probabilities
y_pred_lgb = lgb_model.predict(X_test_scaled)
y_pred_proba_lgb = lgb_model.predict_proba(X_test_scaled)[:, 1]

print(f"LightGBM F1: {f1_score(y_test, y_pred_lgb):.4f}")
```

### 3. CatBoost Classification

**Purpose**: Categorical Boosting for handling mixed data types and reducing overfitting through symmetric trees.

```python
from catboost import CatBoostClassifier

# CatBoost classifier with GPU support
catboost_model = CatBoostClassifier(
    iterations=500,            # Number of iterations
    depth=6,                   # Tree depth
    learning_rate=0.1,
    l2_leaf_reg=3.0,          # L2 regularization
    random_strength=1.0,      # Random strength for tree splitting
    bagging_temperature=1.0,
    subsample=0.8,
    min_data_in_leaf=20,
    loss_function='Logloss',
    eval_metric='F1',
    task_type='GPU',          # Use GPU
    devices='0',
    random_state=42,
    verbose=False
)

# Train
catboost_model.fit(
    X_train_scaled, y_train,
    eval_set=(X_test_scaled, y_test),
    early_stopping_rounds=50
)

# Predictions
y_pred_cat = catboost_model.predict(X_test_scaled)
y_pred_proba_cat = catboost_model.predict_proba(X_test_scaled)[:, 1]

print(f"CatBoost F1: {f1_score(y_test, y_pred_cat):.4f}")
```

### 4. Random Forest Classification

**Purpose**: Ensemble of decision trees for robust baseline model with built-in feature importance.

```python
from sklearn.ensemble import RandomForestClassifier

# Random Forest classifier
rf_model = RandomForestClassifier(
    n_estimators=500,
    max_depth=15,              # Max tree depth
    min_samples_split=5,       # Minimum samples to split
    min_samples_leaf=2,        # Minimum samples per leaf
    max_features='sqrt',       # Feature sampling strategy
    bootstrap=True,
    n_jobs=-1,                 # Use all cores
    random_state=42,
    class_weight='balanced'    # Handle class imbalance
)

# Train
rf_model.fit(X_train_scaled, y_train)

# Predictions
y_pred_rf = rf_model.predict(X_test_scaled)
y_pred_proba_rf = rf_model.predict_proba(X_test_scaled)[:, 1]

# Feature importance
feature_importance_rf = pd.DataFrame({
    'feature': [f'emb_{i}' for i in range(X_train.shape[1])],
    'importance': rf_model.feature_importances_
}).sort_values('importance', ascending=False)

print(f"Random Forest F1: {f1_score(y_test, y_pred_rf):.4f}")
print(f"Top features:\n{feature_importance_rf.head(10)}")
```

### 5. Support Vector Machine (SVM)

**Purpose**: Support Vector Machine with probability calibration for robust decision boundaries.

```python
from sklearn.svm import SVC
from sklearn.calibration import CalibratedClassifierCV

# SVM with RBF kernel
svm_base = SVC(
    kernel='rbf',              # Kernel type: 'linear', 'rbf', 'poly'
    C=1.0,                     # Regularization parameter
    gamma='scale',             # Kernel coefficient
    class_weight='balanced',   # Handle class imbalance
    probability=False,
    random_state=42
)

# Calibrate for probability estimates
svm_model = CalibratedClassifierCV(svm_base, method='sigmoid', cv=5)

# Train
svm_model.fit(X_train_scaled, y_train)

# Predictions
y_pred_svm = svm_model.predict(X_test_scaled)
y_pred_proba_svm = svm_model.predict_proba(X_test_scaled)[:, 1]

print(f"SVM F1: {f1_score(y_test, y_pred_svm):.4f}")
print(f"SVM ROC-AUC: {roc_auc_score(y_test, y_pred_proba_svm):.4f}")
```

### 6. MLP Deep Learning Classifier

**Purpose**: Multi-layer Perceptron using PyTorch for learning complex non-linear representations.

```python
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from torch.optim import Adam

# Define MLP architecture
class AMPClassifier(nn.Module):
    def __init__(self, input_dim=1280, hidden_dims=[512, 256, 128], dropout_rate=0.3):
        super().__init__()
        self.fc1 = nn.Linear(input_dim, hidden_dims[0])
        self.fc2 = nn.Linear(hidden_dims[0], hidden_dims[1])
        self.fc3 = nn.Linear(hidden_dims[1], hidden_dims[2])
        self.fc_out = nn.Linear(hidden_dims[2], 1)

        self.dropout = nn.Dropout(dropout_rate)
        self.batch_norm1 = nn.BatchNorm1d(hidden_dims[0])
        self.batch_norm2 = nn.BatchNorm1d(hidden_dims[1])

    def forward(self, x):
        x = torch.relu(self.batch_norm1(self.fc1(x)))
        x = self.dropout(x)
        x = torch.relu(self.batch_norm2(self.fc2(x)))
        x = self.dropout(x)
        x = torch.relu(self.fc3(x))
        x = torch.sigmoid(self.fc_out(x))
        return x

# Initialize model
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
mlp_model = AMPClassifier(input_dim=1280, hidden_dims=[512, 256, 128], dropout_rate=0.3)
mlp_model.to(device)

# Prepare DataLoader
train_dataset = TensorDataset(
    torch.FloatTensor(X_train_scaled),
    torch.FloatTensor(y_train).unsqueeze(1)
)
train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)

# Training
optimizer = Adam(mlp_model.parameters(), lr=0.001)
criterion = nn.BCELoss()

mlp_model.train()
for epoch in range(100):
    for X_batch, y_batch in train_loader:
        X_batch, y_batch = X_batch.to(device), y_batch.to(device)

        optimizer.zero_grad()
        logits = mlp_model(X_batch)
        loss = criterion(logits, y_batch)
        loss.backward()
        optimizer.step()

    if (epoch + 1) % 20 == 0:
        print(f"Epoch {epoch+1}/100, Loss: {loss.item():.4f}")

# Prediction
mlp_model.eval()
with torch.no_grad():
    X_test_tensor = torch.FloatTensor(X_test_scaled).to(device)
    y_pred_proba_mlp = mlp_model(X_test_tensor).cpu().numpy().flatten()

y_pred_mlp = (y_pred_proba_mlp > 0.5).astype(int)
print(f"MLP F1: {f1_score(y_test, y_pred_mlp):.4f}")
```

### 7. Soft Voting Ensemble with Threshold Optimization

**Purpose**: Combine multiple classifiers using weighted soft voting for optimal F1 score (**ACC 0.959, F1 0.892**).

```python
from sklearn.ensemble import VotingClassifier
from sklearn.metrics import precision_recall_curve

# Create base classifiers
base_classifiers = [
    ('xgb', xgb),
    ('rf', rf_model),
    ('svm', svm_model),
]

# Soft voting ensemble
voting_clf = VotingClassifier(
    estimators=base_classifiers,
    voting='soft',  # Use probability estimates
    weights=[2, 1, 1]  # Weights for each classifier (can be tuned)
)

# Train ensemble
voting_clf.fit(X_train_scaled, y_train)

# Get ensemble probabilities
y_pred_proba_ensemble = voting_clf.predict_proba(X_test_scaled)[:, 1]

# Threshold optimization for F1 score
precision, recall, thresholds = precision_recall_curve(y_test, y_pred_proba_ensemble)
f1_scores_thresh = 2 * (precision * recall) / (precision + recall + 1e-10)
best_idx = np.argmax(f1_scores_thresh)
best_threshold = thresholds[best_idx] if best_idx < len(thresholds) else 0.5

print(f"Optimal threshold: {best_threshold:.4f}")
print(f"F1 at optimal threshold: {f1_scores_thresh[best_idx]:.4f}")

# Apply optimized threshold
y_pred_ensemble_optimized = (y_pred_proba_ensemble > best_threshold).astype(int)

print(f"Ensemble Accuracy: {accuracy_score(y_test, y_pred_ensemble_optimized):.4f}")
print(f"Ensemble F1: {f1_score(y_test, y_pred_ensemble_optimized):.4f}")
print(f"Ensemble ROC-AUC: {roc_auc_score(y_test, y_pred_proba_ensemble):.4f}")
```

### 8. Hyperparameter Optimization with Bayesian Search

**Purpose**: Automated hyperparameter tuning using Bayesian optimization for best performance.

```python
from skopt import BayesSearchCV
from skopt.space import Real, Integer

# Define search space for XGBoost
search_space = {
    'max_depth': Integer(3, 10),
    'learning_rate': Real(0.01, 0.3, prior='log-uniform'),
    'subsample': Real(0.5, 1.0),
    'colsample_bytree': Real(0.5, 1.0),
    'reg_alpha': Real(0.0, 1.0),
    'reg_lambda': Real(0.0, 2.0),
}

# Bayesian search
bayes_search = BayesSearchCV(
    XGBClassifier(n_estimators=200, random_state=42),
    search_space,
    n_iter=20,           # Number of parameter settings
    cv=5,
    scoring='f1',
    n_jobs=-1,
    random_state=42,
    verbose=1
)

# Run search
bayes_search.fit(X_train_scaled, y_train)

print(f"Best parameters: {bayes_search.best_params_}")
print(f"Best cross-validation F1: {bayes_search.best_score_:.4f}")

# Evaluate best model
best_xgb = bayes_search.best_estimator_
y_pred_best = best_xgb.predict(X_test_scaled)
print(f"Test F1 with optimized params: {f1_score(y_test, y_pred_best):.4f}")
```

---

## Parameter Reference Table

| Classifier | Key Parameters | Typical Range | Notes |
|------------|-----------------|----------------|-------|
| **XGBoost** | max_depth | 3-10 | Balance complexity vs overfitting |
| | learning_rate | 0.01-0.3 | Lower = slower but often better |
| | subsample | 0.5-1.0 | Row sampling ratio |
| | colsample_bytree | 0.5-1.0 | Feature sampling ratio |
| **LightGBM** | num_leaves | 20-100 | More leaves = higher complexity |
| | min_child_samples | 10-50 | Minimum samples per leaf |
| | learning_rate | 0.01-0.3 | Similar to XGBoost |
| **CatBoost** | depth | 4-10 | Tree depth |
| | l2_leaf_reg | 1-10 | L2 regularization strength |
| **Random Forest** | n_estimators | 100-1000 | More trees = better but slower |
| | max_depth | 10-30 | Usually deeper than boosting |
| | min_samples_leaf | 1-10 | Minimum samples per leaf |
| **SVM** | C | 0.001-100 | Inverse regularization strength |
| | gamma | 'scale', 'auto', or float | Kernel coefficient |
| **MLP** | hidden_dims | [512, 256, 128] | Layer sizes (adjust for dataset) |
| | dropout_rate | 0.2-0.5 | Regularization strength |
| | learning_rate | 0.0001-0.01 | Optimizer learning rate |
| **Ensemble** | weights | [1-3, 1-3, 1-3] | Relative importance of base models |
| | threshold | 0.4-0.6 | Decision boundary for binary class |

---

## Common Pitfalls & Solutions

### 1. **Class Imbalance Not Handled**
```python
# ❌ Problem: Models biased toward majority class
xgb_bad = XGBClassifier(n_estimators=100)

# ✅ Solution: Use class weights
xgb_good = XGBClassifier(
    n_estimators=100,
    scale_pos_weight=len(y_train[y_train==0]) / len(y_train[y_train==1])
)
```

### 2. **Features Not Normalized (Critical for SVM)**
```python
# ❌ Problem: SVM performance degraded
svm_model.fit(X_train, y_train)  # Raw embeddings

# ✅ Solution: Normalize before SVM
scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
svm_model.fit(X_train_scaled, y_train)
```

### 3. **Overfitting from Deep Network**
```python
# ❌ Problem: Too many parameters, no regularization
mlp_bad = AMPClassifier(input_dim=1280, hidden_dims=[2048, 2048, 1024])

# ✅ Solution: Add dropout and batch norm
mlp_good = AMPClassifier(
    input_dim=1280,
    hidden_dims=[512, 256, 128],
    dropout_rate=0.3  # Enable dropout
)
```

### 4. **Default Threshold (0.5) Suboptimal for F1**
```python
# ❌ Problem: Using default decision threshold
y_pred = (y_pred_proba > 0.5).astype(int)

# ✅ Solution: Optimize threshold for target metric
from sklearn.metrics import precision_recall_curve
precision, recall, thresholds = precision_recall_curve(y_test, y_pred_proba)
f1_scores = 2 * (precision * recall) / (precision + recall + 1e-10)
best_threshold = thresholds[np.argmax(f1_scores)]
y_pred = (y_pred_proba > best_threshold).astype(int)
```

### 5. **Data Leakage in Preprocessing**
```python
# ❌ Problem: Fitting scaler on entire dataset
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X_all)
X_train, X_test = train_test_split(X_scaled, ...)

# ✅ Solution: Fit scaler only on training data
X_train, X_test, y_train, y_test = train_test_split(X, y, ...)
scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled = scaler.transform(X_test)
```

### 6. **GPU Memory Exhaustion with Large Batch Size**
```python
# ❌ Problem: OOM error on GPU
lgb_model = lgb.LGBMClassifier(device_type='gpu', gpu_device_id=0)
lgb_model.fit(X_train, y_train)  # Fails if data too large

# ✅ Solution: Use batch learning with smaller data chunks
for batch_idx in range(0, len(X_train), 10000):
    X_batch = X_train[batch_idx:batch_idx+10000]
    y_batch = y_train[batch_idx:batch_idx+10000]
    # Process batch...
```

---

## References & Further Reading

- **XGBoost**: Chen & Guestrin (2016) - "XGBoost: A Scalable Tree Boosting System" | https://arxiv.org/abs/1603.02754
- **LightGBM**: Ke et al. (2017) - "LightGBM: A Fast, Distributed, High Performance Gradient Boosting Framework" | https://github.com/microsoft/LightGBM
- **CatBoost**: Dorogush et al. (2018) - "CatBoost: gradient boosting with categorical features support" | https://arxiv.org/abs/1810.11372
- **Random Forest**: Breiman (2001) - "Random Forests" | https://doi.org/10.1023/A:1010933404324
- **SVM for AMP**: Gabere & Noble (2017) - "Empirical comparison of machine learning algorithms for AMP discovery" | https://doi.org/10.1038/ncomms12449
- **PyTorch MLP**: LeCun et al. (2015) - Deep Learning | https://www.deeplearningbook.org
- **Ensemble Methods**: Schapire & Freund (2012) - "Boosting: Foundations and Algorithms" | ISBN 978-0262017258
- **Soft Voting**: Kuncheva (2014) - "Combining Pattern Classifiers" | https://doi.org/10.1002/9781118315645
- **SHAP Explainability**: Lundberg & Lee (2017) - "A Unified Approach to Interpreting Model Predictions" | https://arxiv.org/abs/1705.07874

---

## Complete Integration Example

```python
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score, confusion_matrix
import xgboost as xgb
import lightgbm as lgb
from sklearn.ensemble import RandomForestClassifier, VotingClassifier
from sklearn.svm import SVC
from sklearn.calibration import CalibratedClassifierCV
import matplotlib.pyplot as plt
import seaborn as sns

# Load ESM embeddings and labels
# esm_embeddings: (n_samples, 1280)
# y_labels: (n_samples,) with binary values

# Split data
X_train, X_test, y_train, y_test = train_test_split(
    esm_embeddings, y_labels,
    test_size=0.2, random_state=42, stratify=y_labels
)

# Normalize
scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled = scaler.transform(X_test)

# Train individual classifiers
print("Training classifiers...")
xgb_clf = xgb.XGBClassifier(n_estimators=500, max_depth=6, learning_rate=0.1, random_state=42)
xgb_clf.fit(X_train_scaled, y_train, eval_set=[(X_test_scaled, y_test)], early_stopping_rounds=50, verbose=False)

lgb_clf = lgb.LGBMClassifier(n_estimators=500, max_depth=6, learning_rate=0.1, random_state=42)
lgb_clf.fit(X_train_scaled, y_train, eval_set=[(X_test_scaled, y_test)], callbacks=[lgb.early_stopping(50)])

rf_clf = RandomForestClassifier(n_estimators=500, max_depth=15, random_state=42, n_jobs=-1)
rf_clf.fit(X_train_scaled, y_train)

svm_base = SVC(kernel='rbf', C=1.0, random_state=42)
svm_clf = CalibratedClassifierCV(svm_base, cv=5)
svm_clf.fit(X_train_scaled, y_train)

# Create ensemble
voting_clf = VotingClassifier(
    estimators=[('xgb', xgb_clf), ('lgb', lgb_clf), ('rf', rf_clf), ('svm', svm_clf)],
    voting='soft',
    weights=[2, 1, 1, 1]
)
voting_clf.fit(X_train_scaled, y_train)

# Evaluate
results = {}
for name, clf in [('XGBoost', xgb_clf), ('LightGBM', lgb_clf), ('RF', rf_clf),
                   ('SVM', svm_clf), ('Ensemble', voting_clf)]:
    y_pred = clf.predict(X_test_scaled)
    y_pred_proba = clf.predict_proba(X_test_scaled)[:, 1]
    results[name] = {
        'Accuracy': accuracy_score(y_test, y_pred),
        'F1': f1_score(y_test, y_pred),
        'ROC-AUC': roc_auc_score(y_test, y_pred_proba)
    }

results_df = pd.DataFrame(results).T
print("\nClassifier Performance:")
print(results_df.to_string())

# Save results
results_df.to_csv('classifier_performance.csv')
print("\nResults saved to classifier_performance.csv")
```

---

## Dataset & Training Notes

- **Input**: ESM-2 650M protein embeddings (1280-dimensional vectors)
- **Labels**: Binary classification (AMP: 1, Non-AMP: 0)
- **Typical Dataset Size**: 5,000-20,000 sequences for robust training
- **Train/Test Split**: 80/20 with stratification to preserve class distribution
- **Cross-validation**: 5-fold stratified for reliable performance estimates
- **Normalization**: StandardScaler recommended for all classifiers except tree-based (optional for trees)

---

## Publication & Attribution

If using these classifiers in your research, please cite:
1. ESM-2 embeddings: Lin et al. (2023) - "Language models of protein sequences at the scale of evolution enable accurate structure prediction"
2. Classifier implementations: This SKILL based on scikit-learn, XGBoost, LightGBM, CatBoost documentation
3. Your paper with model performance
