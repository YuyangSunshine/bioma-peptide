from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd


_TOP_RE = re.compile(r"Top\s*([1-5])\s*(?:推荐|[:：])", re.IGNORECASE)
_MUT_RE = re.compile(r"\b[A-Z]\d+[A-Z]\b")


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _first_float(patterns: list[str], text: str) -> Optional[float]:
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return _to_float(m.group(1))
    return None


def _split_top_sections(text: str) -> dict[str, str]:
    matches = list(_TOP_RE.finditer(text or ""))
    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        top_id = f"Top{match.group(1)}"
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        section = text[start:end].strip()
        if section and top_id not in sections:
            sections[top_id] = section
    return sections


def _extract_mutations(section: str) -> list[str]:
    mutation_line = ""
    m = re.search(r"突变位点[:：]\s*([^\n\r]+)", section)
    if m:
        mutation_line = m.group(1)
    source = mutation_line or section
    out: list[str] = []
    for mut in _MUT_RE.findall(source):
        if mut not in out:
            out.append(mut)
    return out


def _extract_prediction_candidates(context_text: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for candidate, section in _split_top_sections(context_text).items():
        predicted_gain = _first_float(
            [
                r"Tm\s*(?:提升|增益|gain)[^\+\-\d]{0,30}([+-]?\d+(?:\.\d+)?)\s*(?:℃|°C|C)?",
                r"(?:提升|增益|gain)[^\+\-\d]{0,30}([+-]?\d+(?:\.\d+)?)\s*(?:℃|°C|C)",
            ],
            section,
        )
        rows.append(
            {
                "candidate": candidate,
                "predicted_gain_C": predicted_gain,
                "evoef2_energy": _first_float(
                    [r"EvoEF2[^\+\-\d]{0,30}([+-]?\d+(?:\.\d+)?)"], section
                ),
                "plddt": _first_float(
                    [r"pLDDT\s*[~约]?\s*([+-]?\d+(?:\.\d+)?)"], section
                ),
                "solubility_delta": _first_float(
                    [
                        r"(?:NetSolP|溶解度)[^\+\-\d]{0,30}([+-]?\d+(?:\.\d+)?)",
                        r"溶解度提升\s*([+-]?\d+(?:\.\d+)?)",
                    ],
                    section,
                ),
                "epistasis_delta": _first_float(
                    [
                        r"(?:epistasis\s*delta|上位效应)[^\+\-\d]{0,30}([+-]?\d+(?:\.\d+)?)",
                    ],
                    section,
                ),
                "mutations": ";".join(_extract_mutations(section)),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    order = {f"Top{i}": i for i in range(1, 6)}
    return df.sort_values(
        "candidate", key=lambda s: s.map(order).fillna(99)
    ).reset_index(drop=True)


def _find_group_stats_path(analysis_artifacts: dict[str, Any]) -> str:
    lightcycler = analysis_artifacts.get("lightcycler_melt") or {}
    path = str(lightcycler.get("group_tm_statistics") or "")
    return path if path and Path(path).exists() else ""


def _load_group_stats(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=object, encoding="utf-8-sig")
    if "group" not in df.columns:
        df["group"] = ""
    df["group_norm"] = df["group"].astype(str).str.replace(" ", "", regex=False)
    return df


def _positive_control_mean(stats: pd.DataFrame) -> Optional[float]:
    if stats.empty:
        return None
    group = (
        stats["group"].astype(str).str.lower()
        if "group" in stats.columns
        else pd.Series([""] * len(stats), index=stats.index)
    )
    role = (
        stats["role"].astype(str).str.lower()
        if "role" in stats.columns
        else pd.Series([""] * len(stats), index=stats.index)
    )
    control = stats[
        group.isin({"positive_control", "positivecontrol", "r6", "baseline"})
        | role.str.contains("positive")
    ]
    if control.empty:
        return None
    return _to_float(control.iloc[0].get("mean_Tm"))


def _observed_candidate_stats(stats: pd.DataFrame) -> pd.DataFrame:
    if stats.empty:
        return pd.DataFrame()
    baseline = _positive_control_mean(stats)
    rows: list[dict[str, Any]] = []
    for _, row in stats.iterrows():
        group = str(row.get("group") or "").strip()
        m = re.fullmatch(r"Top\s*([1-5])", group, flags=re.IGNORECASE)
        if not m:
            continue
        mean_tm = _to_float(row.get("mean_Tm"))
        observed_gain = _to_float(row.get("delta_vs_positive_control"))
        if observed_gain is None and mean_tm is not None and baseline is not None:
            observed_gain = mean_tm - baseline
        rows.append(
            {
                "candidate": f"Top{m.group(1)}",
                "observed_mean_Tm_C": mean_tm,
                "observed_gain_C": observed_gain,
                "n": int(_to_float(row.get("n")) or 0),
                "sd_Tm": _to_float(row.get("sd_Tm")),
                "sem_Tm": _to_float(row.get("sem_Tm")),
                "baseline_mean_Tm_C": baseline,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    order = {f"Top{i}": i for i in range(1, 6)}
    return df.sort_values(
        "candidate", key=lambda s: s.map(order).fillna(99)
    ).reset_index(drop=True)


def _classify_residual(residual: Optional[float]) -> tuple[str, str]:
    if residual is None or not math.isfinite(residual):
        return "not_evaluable", "not_evaluable"
    abs_res = abs(residual)
    if abs_res >= 4:
        level = "high"
    elif abs_res >= 3:
        level = "moderate"
    elif abs_res >= 2:
        level = "mild"
    else:
        level = "none"
    if residual < 0:
        direction = "prediction_overestimated"
    elif residual > 0:
        direction = "prediction_underestimated"
    else:
        direction = "matched"
    return level, direction


def _build_review(pred: pd.DataFrame, obs: pd.DataFrame) -> pd.DataFrame:
    merged = pred.merge(obs, on="candidate", how="inner")
    if merged.empty:
        return merged
    residuals: list[Optional[float]] = []
    levels: list[str] = []
    directions: list[str] = []
    for _, row in merged.iterrows():
        pred_gain = _to_float(row.get("predicted_gain_C"))
        obs_gain = _to_float(row.get("observed_gain_C"))
        residual = (
            None if pred_gain is None or obs_gain is None else obs_gain - pred_gain
        )
        level, direction = _classify_residual(residual)
        residuals.append(residual)
        levels.append(level)
        directions.append(direction)
    merged["residual_C"] = residuals
    merged["anomaly_level"] = levels
    merged["direction"] = directions
    cols = [
        "candidate",
        "predicted_gain_C",
        "observed_mean_Tm_C",
        "baseline_mean_Tm_C",
        "observed_gain_C",
        "residual_C",
        "anomaly_level",
        "direction",
        "n",
        "sd_Tm",
        "sem_Tm",
        "evoef2_energy",
        "plddt",
        "solubility_delta",
        "epistasis_delta",
        "mutations",
    ]
    for col in cols:
        if col not in merged.columns:
            merged[col] = None
    return merged[cols]


def _hypothesis_seeds(review: pd.DataFrame) -> list[str]:
    seeds: list[str] = []
    if review.empty:
        return seeds
    high_or_moderate = review[review["anomaly_level"].isin(["high", "moderate"])]
    over = review[review["direction"].eq("prediction_overestimated")]
    if not over.empty:
        seeds.append("实测增益低于预测增益，提示模型可能高估了多突变组合的稳定化贡献。")
    if not high_or_moderate.empty:
        seeds.append(
            "残差达到中高等级，适合在报告中作为异常候选进行反思，而不是直接判定失败原因。"
        )
    mutation_load = (
        review["mutations"]
        .astype(str)
        .str.split(";")
        .map(lambda x: len([v for v in x if v]))
    )
    if (mutation_load >= 7).any():
        seeds.append(
            "候选包含较多突变位点，可提出负上位效应或突变组合互相抵消作为待验证假设。"
        )
    plddt = pd.to_numeric(review.get("plddt"), errors="coerce")
    if plddt.notna().any() and (plddt < 50).any():
        seeds.append("pLDDT 偏低时，结构模型置信度不足可作为预测偏差的可能来源。")
    return seeds


def _next_actions(review: pd.DataFrame) -> list[str]:
    actions = [
        "优先复核残差最大的候选，确认该组重复孔、QC 与主峰提取是否稳定。",
        "将实测表现最好的候选作为下一轮设计 parent，而不是只按预测增益排序。",
    ]
    if not review.empty and review["anomaly_level"].isin(["high", "moderate"]).any():
        actions.append(
            "对残差较大的候选做组合拆分或回退验证，用实验区分负上位效应与模型评分偏差。"
        )
    return actions


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _summary(review: pd.DataFrame) -> dict[str, Any]:
    if review.empty:
        return {"n_candidates": 0}
    numeric_residual = pd.to_numeric(review["residual_C"], errors="coerce")
    largest_idx = (
        numeric_residual.abs().idxmax() if numeric_residual.notna().any() else None
    )
    largest = review.loc[largest_idx].to_dict() if largest_idx is not None else {}
    return _jsonable(
        {
            "n_candidates": int(len(review)),
            "largest_abs_residual_candidate": largest.get("candidate"),
            "largest_abs_residual_C": None
            if largest_idx is None
            else float(abs(numeric_residual.loc[largest_idx])),
            "n_prediction_overestimated": int(
                review["direction"].eq("prediction_overestimated").sum()
            ),
            "n_prediction_underestimated": int(
                review["direction"].eq("prediction_underestimated").sum()
            ),
            "n_moderate_or_high_anomalies": int(
                review["anomaly_level"].isin(["moderate", "high"]).sum()
            ),
            "candidate_rows": review.to_dict(orient="records"),
            "hypothesis_seeds": _hypothesis_seeds(review),
            "next_actions": _next_actions(review),
            "interpretation_guardrail": "Hypotheses are explanatory possibilities generated from residual patterns; do not state them as confirmed causal mechanisms without additional validation.",
        }
    )


def run_prediction_review(
    *,
    context_text: str,
    analysis_artifacts: dict[str, Any],
    outputs_dir: str,
) -> dict[str, Any]:
    stats_path = _find_group_stats_path(analysis_artifacts)
    if not stats_path:
        return {
            "status": "skipped",
            "reason": "No LightCycler group statistics artifact found.",
        }

    pred = _extract_prediction_candidates(context_text or "")
    if pred.empty or pred["predicted_gain_C"].isna().all():
        return {
            "status": "skipped",
            "reason": "No Top1-Top5 predicted Tm gains found in context text.",
        }

    obs = _observed_candidate_stats(_load_group_stats(stats_path))
    if obs.empty:
        return {
            "status": "skipped",
            "reason": "No Top1-Top5 observed group statistics found.",
        }

    review = _build_review(pred, obs)
    if review.empty:
        return {
            "status": "skipped",
            "reason": "Predicted candidates and observed groups did not overlap.",
        }

    out_dir = Path(outputs_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "r7_prediction_vs_observed.csv"
    json_path = out_dir / "r7_prediction_review_summary.json"

    review.to_csv(csv_path, index=False, encoding="utf-8-sig")
    summary = _summary(review)
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "status": "success",
        "analysis_id": "r7_prediction_review",
        "output_files": [str(csv_path), str(json_path)],
        "artifacts": {
            "prediction_vs_observed": str(csv_path),
            "prediction_review_summary": str(json_path),
            "group_tm_statistics": stats_path,
        },
        "summary": summary,
    }


__all__ = ["run_prediction_review"]
