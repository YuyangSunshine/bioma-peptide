from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChartSkill:
    key: str
    display_name: str
    description: str
    instrument_family: str
    trigger_terms: tuple[str, ...]
    required_roles: tuple[str, ...]
    optional_roles: tuple[str, ...] = ()
    planning_hint: str = ""
    plot_hint: str = ""
    analysis_hint: str = ""
    editable_config: dict[str, Any] = field(default_factory=dict)


COMMON_LAYOUT_CONFIG: dict[str, Any] = {
    "title_fontsize": 14,
    "title_position": {"x": 0.5, "y": 1.0},
    "x_label_fontsize": 12,
    "y_label_fontsize": 12,
    "axis_title_offset_px": {"x": 32, "y": 44},
    "figure_width_px": 520,
    "figure_height_px": 360,
    "export_width_px": 1040,
    "export_height_px": 720,
    "margins_px": {"left": 80, "right": 40, "top": 80, "bottom": 80},
    "legend": {"show": True, "position": "best", "orientation": "vertical"},
}


COMMON_LINE_CONFIG: dict[str, Any] = {
    "trace_names": {},
    "line_colors": {
        "Sample-1": "#4E79A7",
        "Sample-2": "#F28E2B",
        "Sample-3": "#59A14F",
        "Sample-4": "#E15759",
    },
    "line_style": "solid",
    "line_width": 2.0,
    "line_alpha": 1.0,
    "curve_smoothing": "spline",
    "smoothing_factor": 0.35,
}


QPCR_RELATIVE_EXPRESSION_BAR = ChartSkill(
    key="qpcr_relative_expression_bar",
    display_name="Gene Relative Expression Bar Chart",
    description=(
        "Grouped bar chart for qPCR or gene-expression relative expression "
        "values across genes, targets, samples, conditions, or treatment groups."
    ),
    instrument_family="qpcr",
    trigger_terms=(
        "relative expression",
        "gene expression",
        "fold change",
        "2^-ddct",
        "2^-ΔΔCt",
        "qpcr",
        "rt-qpcr",
        "基因相对表达量",
        "相对表达量",
        "表达量柱状图",
        "差异倍数",
    ),
    required_roles=("gene", "group", "value"),
    optional_roles=(
        "error",
        "p_value",
        "significance",
        "label",
        "sample",
        "ct",
        "reference_gene",
        "control_group",
    ),
    planning_hint=(
        "Use this chart when the user asks for gene relative expression, "
        "fold-change, 2^-ΔΔCt, or qPCR grouped comparison plots. Prefer it "
        "over a generic bar chart when columns represent genes/targets, "
        "experimental groups, and relative expression values. If the input is "
        "raw Ct/Cq data, include sample, ct, reference_gene, and control_group "
        "in the analysis plan, compute ΔCt, ΔΔCt, and 2^-ΔΔCt first, and bind "
        "the chart to the analysis output that contains relative_expression or "
        "fold_change. Do not set chart_spec.y to Ct/Cq; Ct/Cq is an input for "
        "analysis, not the bar height. The derived result table must retain the "
        "explicit target identity in a target_gene or gene column; do not rely on "
        "a generic label."
    ),
    plot_hint=(
        "- Create a grouped bar chart.\n"
        "- X-axis is gene or target.\n"
        "- Bar groups represent condition/group/sample, such as Control, Exp-1, "
        "Wild type, Mutant1.\n"
        "- Y-axis is relative expression or fold-change value.\n"
        "- For raw Ct/Cq inputs, derive ΔCt, ΔΔCt, and 2^-ΔΔCt only when "
        "reference_gene and control_group are explicitly provided.\n"
        "- Draw error bars when SD, SEM, CI, or error columns exist.\n"
        "- Draw value labels above bars when possible.\n"
        "- Draw significance annotations only when p-value or significance "
        "columns are provided.\n"
        "- Use editable style settings for title, axis labels, figure size, "
        "margins, legend, bar width, group colors, bar border, hatch pattern, "
        "errorbar width, cap size, annotation font, and significance height."
    ),
    analysis_hint=(
        "- Summarize relative expression differences by gene and group.\n"
        "- Mention the highest and lowest expression groups per gene when clear.\n"
        "- Interpret error bars as variability/uncertainty only when the source "
        "column indicates SD, SEM, CI, or similar.\n"
        "- Interpret significance only from provided p-values or significance "
        "labels. Do not invent statistical significance."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        "title": "Relative Expression of Multiple Genes",
        "x_label": "Genes",
        "y_label": "Relative Expression",
        "bar_width": 0.6,
        "group_colors": {
            "Control": "#4E79A7",
            "Exp-1": "#F28E2B",
            "Exp-2": "#59A14F",
            "Exp-3": "#E15759",
        },
        "bar_edge_color": "#000000",
        "bar_edge_width": 0.0,
        "bar_hatch": "",
        "show_value_labels": True,
        "value_label_fontsize": 12,
        "value_label_color": "#000000",
        "show_error_bars": True,
        "errorbar_linewidth": 2.0,
        "errorbar_capsize": 6,
        "annotation_fontsize": 12,
        "annotation_color": "#444444",
        "annotation_offset_px": {"x": 0, "y": 0},
        "significance_height": 1.07,
    },
)


QPCR_MELT_CURVE_NEG_DERIVATIVE = ChartSkill(
    key="qpcr_melt_curve_neg_derivative",
    display_name="Gene Mutation Melt Curve",
    description=(
        "Multi-trace melt curve for gene mutation or genotyping assays, usually "
        "temperature versus fluorescence or negative derivative signal."
    ),
    instrument_family="qpcr",
    trigger_terms=(
        "melt curve",
        "melting curve",
        "dissociation curve",
        "-dF/dT",
        "negative derivative",
        "Tm",
        "melting temperature",
        "mutation melt",
        "基因突变溶解曲线",
        "溶解曲线",
        "熔解曲线",
        "峰注释",
    ),
    required_roles=("temperature", "value", "series"),
    optional_roles=(
        "fluorescence",
        "derivative",
        "tm",
        "peak_label",
        "peak_count",
        "peak_type",
        "genotype",
        "sample",
    ),
    planning_hint=(
        "Use this chart for qPCR melt/melting/dissociation curves, especially "
        "gene mutation assays. Map temperature to x, fluorescence or -dF/dT to y, "
        "and well/sample/genotype to series."
    ),
    plot_hint=(
        "- Create a multi-line melt curve sorted by temperature.\n"
        "- X-axis is temperature; Y-axis is fluorescence or -dF/dT.\n"
        "- If negative derivative is absent and raw fluorescence exists, compute "
        "-dF/dT before plotting when the analysis stage provides enough data.\n"
        "- Group traces by well, sample, mutation, genotype, or target.\n"
        "- Do not place Tm text labels directly on dense multi-trace plots; report Tm "
        "values in the analysis table/report instead. Add peak annotations only when "
        "the user explicitly requests them or the trace count is small.\n"
        "- Report peak_count and peak_type as no_clear_peak, single_peak, or multi_peak "
        "when derivative peaks are computable; do not infer genotype calls without references.\n"
        "- Apply editable settings for title, axes, figure size, margins, legend, "
        "trace name, peak annotation visibility, line color, line style, line width, "
        "and smoothing factor."
    ),
    analysis_hint=(
        "- Estimate dominant Tm/peak temperature from provided derivative signal "
        "or computed -dF/dT.\n"
        "- Flag multiple peaks or weak/no melt peaks conservatively using peak_count "
        "and peak_type only.\n"
        "- Do not infer mutation calls unless genotype thresholds or labels are provided."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        **COMMON_LINE_CONFIG,
        "title": "Gene Mutation Melt Curve",
        "x_label": "Temperature",
        "y_label": "-dF/dT",
        "show_peak_annotations": False,
        "peak_annotation_fontsize": 12,
        "peak_annotation_color": "#444444",
    },
)


PLATE_FRET_TIME_CURVE = ChartSkill(
    key="plate_fret_time_curve",
    display_name="FRET Curve",
    description=(
        "Multi-trace FRET curve for plate-reader or fluorescence assays, usually "
        "time or cycle versus FRET ratio/intensity."
    ),
    instrument_family="plate_reader",
    trigger_terms=(
        "fret",
        "fret curve",
        "fret ratio",
        "mP",
        "parallel",
        "perpendicular",
        "donor",
        "acceptor",
        "time curve",
        "fluorescence resonance energy transfer",
        "FRET曲线",
        "轨迹设置",
        "垂直线",
    ),
    required_roles=("time", "value", "series"),
    optional_roles=(
        "donor_signal",
        "acceptor_signal",
        "ratio",
        "mp",
        "parallel_signal",
        "perpendicular_signal",
        "annotation",
        "vline",
        "label",
    ),
    planning_hint=(
        "Use this chart for FRET curves. Prefer time as x; use FRET ratio, ratio_value, "
        "mP, or polarization value as y; group by sample, well, treatment, or trace. "
        "For SpectraMax polarization-style exports with parallel(P) and perpendicular(S) "
        "signals, plan mP = (P - S) / (P + S) * 1000."
    ),
    plot_hint=(
        "- Create a multi-line FRET curve sorted by time or cycle.\n"
        "- Prefer a precomputed FRET ratio or mP column.\n"
        "- For SpectraMax polarization-style data, use mP = (P - S) / (P + S) * 1000 "
        "when parallel(P) and perpendicular(S) signals are present.\n"
        "- Do not infer donor/acceptor ratio direction unless a ratio formula is provided.\n"
        "- Group traces by sample, well, treatment, or channel.\n"
        "- Add vertical markers or annotations only when event/vline columns exist.\n"
        "- Apply editable settings for title, axes, figure size, margins, legend "
        "orientation, curve smoothing algorithm, smoothing factor, line alpha, "
        "trace name, line color, per-trace legend visibility, and annotations."
    ),
    analysis_hint=(
        "- Summarize FRET trend, peak/endpoint response, and treatment differences "
        "only from provided or computed ratio/intensity values.\n"
        "- Avoid mechanistic claims unless the experiment metadata states the assay design."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        **COMMON_LINE_CONFIG,
        "title": "FRET Curve",
        "x_label": "Time",
        "y_label": "FRET Ratio",
        "per_trace_legend": {},
        "show_vertical_lines": False,
        "vertical_lines": [],
        "annotation_fontsize": 12,
        "annotation_color": "#444444",
    },
)


QPCR_AMPLIFICATION_CURVE = ChartSkill(
    key="qpcr_amplification_curve",
    display_name="PCR Amplification Curve",
    description=(
        "Multi-trace qPCR amplification curve for cycle-by-cycle fluorescence "
        "signals, Ct/Cp annotation, and threshold-line visualization."
    ),
    instrument_family="qpcr",
    trigger_terms=(
        "amplification curve",
        "pcr amplification",
        "qpcr amplification",
        "cycle",
        "ct",
        "cp",
        "threshold",
        "ΔRn",
        "扩增曲线",
        "PCR扩增曲线",
        "阈值线",
        "Ct标注",
    ),
    required_roles=("cycle", "value", "series"),
    optional_roles=("ct", "cp", "threshold", "baseline", "annotation", "label"),
    planning_hint=(
        "Use this chart for qPCR/PCR amplification curves. Map cycle to x, "
        "fluorescence or ΔRn to y, and sample/well/target/replicate to series. "
        "Include Ct/Cp and threshold annotations only when supported by data."
    ),
    plot_hint=(
        "- Create a multi-line amplification curve sorted by cycle.\n"
        "- X-axis is cycle; Y-axis is fluorescence, ΔRn, Rn, or intensity.\n"
        "- Group traces by sample, well, target, dye, or replicate.\n"
        "- Draw Ct/Cp labels only when Ct/Cp columns or computed crossing points exist.\n"
        "- Draw threshold line only when threshold mode/value is provided or computed.\n"
        "- Apply editable settings for title, axes, figure size, margins, legend, "
        "smoothing mode, smoothing factor, global line alpha, trace name, Ct "
        "annotation content/font/size/color/position, line color, line width, "
        "threshold line visibility/value/color/width/style/layer, and per-trace labels."
    ),
    analysis_hint=(
        "- Evaluate amplification shape and basic QC flags.\n"
        "- Flag flat curves, late amplification, abnormal oscillation, or missing "
        "threshold crossing conservatively.\n"
        "- Do not report Ct/Cp values unless they are provided or explicitly computed."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        **COMMON_LINE_CONFIG,
        "title": "PCR Amplification Curve",
        "x_label": "Cycle",
        "y_label": "ΔRn",
        "show_curve_annotations": True,
        "curve_annotation_field": "Ct",
        "curve_annotation_font": "Arial",
        "curve_annotation_fontsize": 12,
        "curve_annotation_color": "#444444",
        "curve_annotation_position": "auto",
        "threshold_line": {
            "show": True,
            "mode": "ct",
            "layer": "above",
            "y": None,
            "color": "#777777",
            "width": 1.5,
            "style": "dash",
            "legend_label": "Threshold",
        },
        "per_trace_labels": {},
    },
)


PLATE_STANDARD_CURVE = ChartSkill(
    key="plate_standard_curve",
    display_name="Standard Quantitation Curve",
    description=(
        "Standard quantitation curve for plate-reader assays, including standard "
        "points, fitted line, LLOQ/ULOQ helper lines, and equation annotation."
    ),
    instrument_family="plate_reader",
    trigger_terms=(
        "standard curve",
        "standard quantitation",
        "calibration curve",
        "quantitation curve",
        "lloq",
        "uloq",
        "equation",
        "r squared",
        "标准曲线",
        "标准定量图",
        "标准定量曲线",
        "方程式标注",
    ),
    required_roles=("concentration", "response"),
    optional_roles=(
        "series",
        "sample_type",
        "error",
        "fit_value",
        "lloq",
        "uloq",
        "equation",
        "r_squared",
    ),
    planning_hint=(
        "Use this chart for standard/calibration/quantitation curves. Map known "
        "standard concentration to x and endpoint signal such as OD450/response to y. "
        "Plan fit_model='auto' unless the user explicitly requests a model; auto may "
        "compare linear, logarithmic, exponential, power, polynomial2, and inverse fits."
    ),
    plot_hint=(
        "- Create a scatter plot of standards plus a fitted standard curve when "
        "enough numeric points exist.\n"
        "- X-axis is concentration; Y-axis is response/signal/OD.\n"
        "- Show standard sample markers and error bars when replicate/error columns exist.\n"
        "- Draw fitted curve using analysis outputs when available; do not invent coefficients.\n"
        "- Use fit_model/equation/r_squared/adjusted_r_squared from the chart-ready data "
        "when present.\n"
        "- Add LLOQ/ULOQ vertical helper lines only when provided or computed.\n"
        "- Add equation and R-squared annotation only when available from fitting.\n"
        "- Apply editable settings for title, title color, axes, figure size, margins, "
        "legend orientation, standard visibility/name/marker color/marker size, "
        "standard error bars, fitted curve name/style/color/width, helper lines, "
        "and equation annotation."
    ),
    analysis_hint=(
        "- Evaluate standard curve fit quality, quantitation range, and obvious outliers.\n"
        "- Report selected fit_model, candidate models, fit coefficients, R-squared, "
        "adjusted R-squared, RMSE, LLOQ, and ULOQ only when computed or supplied.\n"
        "- Avoid back-calculating unknown samples unless the table has sample responses."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        "title": "Standard Curve",
        "title_color": "#000000",
        "x_label": "Concentration (ng/mL)",
        "y_label": "Response (OD450)",
        "fit_model": "auto",
        "candidate_fit_models": [
            "linear",
            "logarithmic",
            "exponential",
            "power",
            "polynomial2",
            "inverse",
        ],
        "standard_points": {
            "show": True,
            "legend_name": "Standards",
            "marker_color": "#4E79A7",
            "marker_size": 7,
            "show_error_bars": True,
            "show_legend": True,
        },
        "fit_curve": {
            "legend_name": "Fit",
            "line_style": "solid",
            "line_color": "#E15759",
            "line_width": 2.0,
            "show_legend": True,
        },
        "helper_lines": {
            "show_lloq": True,
            "show_uloq": True,
            "line_color": "#777777",
            "line_width": 1.2,
            "line_style": "dash",
        },
        "equation_annotation": {
            "show": True,
            "text": "",
            "fontsize": 12,
            "color": "#444444",
            "position": {"x": 0.05, "y": 0.95},
        },
    },
)


PLATE_MICHAELIS_MENTEN_CURVE = ChartSkill(
    key="plate_michaelis_menten_curve",
    display_name="Michaelis-Menten Curve",
    description=(
        "Enzyme kinetics Michaelis-Menten curve with substrate concentration, "
        "initial velocity, fitted curve, confidence band, Km/Vmax helper lines, "
        "and labels."
    ),
    instrument_family="plate_reader",
    trigger_terms=(
        "michaelis",
        "michaelis-menten",
        "enzyme kinetics",
        "substrate concentration",
        "initial velocity",
        "km",
        "vmax",
        "米氏曲线",
        "米氏方程",
        "酶动力学",
        "底物浓度",
        "初速度",
    ),
    required_roles=("substrate_concentration", "velocity"),
    optional_roles=(
        "series",
        "error",
        "fit_value",
        "confidence_band",
        "km",
        "vmax",
        "label",
    ),
    planning_hint=(
        "Use this chart for Michaelis-Menten enzyme kinetics. Map substrate "
        "concentration [S] to x and initial velocity v0 to y. Plan nonlinear "
        "fit for Km and Vmax when enough points exist."
    ),
    plot_hint=(
        "- Create a scatter plot of velocity versus substrate concentration.\n"
        "- Overlay a Michaelis-Menten fitted curve only when fit outputs or enough "
        "numeric points are available.\n"
        "- Show error bars, confidence band, and scatter legend only when supported.\n"
        "- Draw Km and Vmax helper lines/labels only from supplied or fitted values.\n"
        "- Apply editable settings for title, title color, axes, figure size, margins, "
        "legend orientation, scatter legend name/color/size, error bars, confidence "
        "band, curve legend name/style/color/width, and Km/Vmax helper annotations."
    ),
    analysis_hint=(
        "- Estimate Km and Vmax only with sufficient concentration and velocity data.\n"
        "- Report fit uncertainty only when confidence intervals or covariance are available.\n"
        "- Mention saturation trend and outliers conservatively."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        "title": "Michaelis-Menten Curve",
        "title_color": "#000000",
        "x_label": "Substrate concentration [S] (uM)",
        "y_label": "Initial velocity v0 (umol/min)",
        "scatter_points": {
            "legend_name": "Observed",
            "marker_color": "#4E79A7",
            "marker_size": 7,
            "show_error_bars": True,
            "show_confidence_band": True,
            "show_legend": True,
        },
        "fit_curve": {
            "legend_name": "Michaelis-Menten fit",
            "line_style": "solid",
            "line_color": "#E15759",
            "line_width": 2.0,
            "show_legend": True,
        },
        "helper_annotations": {
            "show_km": True,
            "show_vmax": True,
            "km_text": "Km",
            "vmax_text": "Vmax",
            "fontsize": 12,
            "color": "#444444",
            "line_color": "#777777",
            "line_style": "dash",
            "line_width": 1.2,
        },
    },
)


PLATE_SAMPLE_CONCENTRATION_BAR = ChartSkill(
    key="plate_sample_concentration_bar",
    display_name="Sample Concentration Bar Chart",
    description=(
        "Grouped bar chart for plate-reader sample concentration or endpoint "
        "measurements after standard-curve quantitation."
    ),
    instrument_family="plate_reader",
    trigger_terms=(
        "sample concentration",
        "concentration bar",
        "sample quantitation",
        "endpoint concentration",
        "ng/ml",
        "ug/ml",
        "difference comparison",
        "group comparison",
        "sample concentration bar",
    ),
    required_roles=("sample", "value"),
    optional_roles=("group", "error", "replicate", "p_value", "significance", "label"),
    planning_hint=(
        "Use this chart for comparing measured or back-calculated sample "
        "concentrations across samples, conditions, or groups. If raw replicate "
        "rows are provided, summarize by sample/group before plotting."
    ),
    plot_hint=(
        "- Create a grouped bar chart.\n"
        "- X-axis is sample or group; Y-axis is concentration or endpoint value.\n"
        "- Aggregate replicate rows as mean with SD when replicate data exist.\n"
        "- Draw error bars only from provided or computed SD/SEM/CI columns.\n"
        "- Draw significance annotations only from provided p-values or labels."
    ),
    analysis_hint=(
        "- Compare sample/group concentrations descriptively.\n"
        "- Report mean, SD, n, and CV when replicate rows are available.\n"
        "- Do not invent statistical significance without p-values or explicit labels."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        "title": "Sample Concentration",
        "x_label": "Sample",
        "y_label": "Concentration",
        "bar_width": 0.6,
        "show_error_bars": True,
        "show_value_labels": True,
        "group_colors": {
            "Control": "#4E79A7",
            "Treatment": "#59A14F",
            "Sample": "#F28E2B",
        },
    },
)


CE_DNA_ELECTROPHEROGRAM = ChartSkill(
    key="ce_dna_electropherogram",
    display_name="DNA Electropherogram",
    description=(
        "Capillary electrophoresis DNA electropherogram showing fragment size "
        "versus fluorescence/intensity, with peak metrics."
    ),
    instrument_family="capillary_electrophoresis",
    trigger_terms=(
        "dna electropherogram",
        "electropherogram",
        "labchip",
        "fragment size",
        "size bp",
        "gqs",
        "ngs library",
        "dna peak",
    ),
    required_roles=("size_bp", "intensity", "series"),
    optional_roles=(
        "peak_size_bp",
        "peak_height",
        "peak_area",
        "concentration",
        "gqs",
        "marker",
        "label",
    ),
    planning_hint=(
        "Use this chart for LabChip or capillary-electrophoresis DNA traces. "
        "Map fragment size in bp to x, RFU/intensity to y, and sample/lane to series."
    ),
    plot_hint=(
        "- Create an electropherogram line/area chart sorted by size_bp.\n"
        "- X-axis is fragment size in bp; Y-axis is RFU/intensity.\n"
        "- Group traces by sample or lane.\n"
        "- Mark main peaks only when peak metrics are provided or computed."
    ),
    analysis_hint=(
        "- Report main peak size, peak height, and approximate peak area by sample.\n"
        "- Mention broad smear or multi-peak patterns conservatively.\n"
        "- Do not assign pass/fail quality unless thresholds are provided."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        **COMMON_LINE_CONFIG,
        "title": "DNA Electropherogram",
        "x_label": "Size (bp)",
        "y_label": "Fluorescence (RFU)",
        "show_peak_annotations": True,
    },
)


CE_RNA_ELECTROPHEROGRAM = ChartSkill(
    key="ce_rna_electropherogram",
    display_name="RNA Electropherogram",
    description=(
        "Capillary electrophoresis RNA electropherogram showing RNA fragment "
        "size versus fluorescence/intensity, with 18S/28S and integrity metrics."
    ),
    instrument_family="capillary_electrophoresis",
    trigger_terms=(
        "rna electropherogram",
        "rna integrity",
        "rqs",
        "rin",
        "18s",
        "28s",
        "rna peak",
        "labchip rna",
    ),
    required_roles=("size_nt", "intensity", "series"),
    optional_roles=(
        "peak_size_nt",
        "peak_height",
        "peak_area",
        "rqs",
        "rin",
        "ratio_28s_18s",
        "marker",
        "label",
    ),
    planning_hint=(
        "Use this chart for LabChip RNA electropherograms. Map size in nt to x, "
        "RFU/intensity to y, and sample/lane to series."
    ),
    plot_hint=(
        "- Create an RNA electropherogram line/area chart sorted by size_nt.\n"
        "- X-axis is RNA size in nt; Y-axis is RFU/intensity.\n"
        "- Group traces by sample or lane.\n"
        "- Annotate 18S/28S peaks only when they are present or computed."
    ),
    analysis_hint=(
        "- Report 18S and 28S peak candidates and the 28S/18S ratio when computable.\n"
        "- Mention degradation or missing peak patterns conservatively.\n"
        "- Do not assign RIN/RQS pass/fail unless thresholds are provided."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        **COMMON_LINE_CONFIG,
        "title": "RNA Electropherogram",
        "x_label": "Size (nt)",
        "y_label": "Fluorescence (RFU)",
        "show_peak_annotations": True,
    },
)


CE_VIRTUAL_GEL = ChartSkill(
    key="ce_virtual_gel",
    display_name="Virtual Gel",
    description=(
        "LabChip-style virtual gel view summarizing capillary-electrophoresis "
        "band intensity by sample/lane and fragment size."
    ),
    instrument_family="capillary_electrophoresis",
    trigger_terms=(
        "virtual gel",
        "gel view",
        "labchip gel",
        "lane",
        "ladder",
        "band",
        "smear",
    ),
    required_roles=("lane", "size", "intensity"),
    optional_roles=(
        "sample",
        "marker",
        "band_size",
        "band_intensity",
        "quality_label",
        "smear_index",
    ),
    planning_hint=(
        "Use this chart for virtual gel representations from LabChip or CE data. "
        "Map lane/sample to x, fragment size to y, and intensity to image darkness."
    ),
    plot_hint=(
        "- Create a virtual-gel heatmap or lane plot.\n"
        "- X-axis is sample/lane; Y-axis is fragment size.\n"
        "- Intensity should control band darkness or heatmap color.\n"
        "- Keep ladder/marker lanes separate when labeled."
    ),
    analysis_hint=(
        "- Report main band size and intensity by lane.\n"
        "- Flag smear-like broad distributions only from the observed intensity profile.\n"
        "- Do not infer sample identity beyond provided sample/lane labels."
    ),
    editable_config={
        **COMMON_LAYOUT_CONFIG,
        "title": "Virtual Gel",
        "x_label": "Lane",
        "y_label": "Size",
        "gel_colormap": "gray_r",
        "show_ladder": True,
        "show_band_labels": True,
    },
)


_CHART_SKILLS = {
    skill.key: skill
    for skill in (
        QPCR_RELATIVE_EXPRESSION_BAR,
        QPCR_MELT_CURVE_NEG_DERIVATIVE,
        PLATE_FRET_TIME_CURVE,
        QPCR_AMPLIFICATION_CURVE,
        PLATE_STANDARD_CURVE,
        PLATE_MICHAELIS_MENTEN_CURVE,
        PLATE_SAMPLE_CONCENTRATION_BAR,
        CE_DNA_ELECTROPHEROGRAM,
        CE_RNA_ELECTROPHEROGRAM,
        CE_VIRTUAL_GEL,
    )
}


def get_chart_skill(chart_key: str) -> ChartSkill | None:
    return _CHART_SKILLS.get(str(chart_key or "").strip())


def all_chart_skills() -> list[ChartSkill]:
    return list(_CHART_SKILLS.values())


def chart_skill_keys() -> set[str]:
    return set(_CHART_SKILLS)
