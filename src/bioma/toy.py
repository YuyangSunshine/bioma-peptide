from __future__ import annotations

import json
import re

from bioma.config import LLMConfig
from bioma.llm import ChatProvider, ChatResponse, ToolCall


class ToyChatProvider(ChatProvider):
    """Deterministic LLM fixture that still uses the production chat contract."""

    def __init__(self, model: str) -> None:
        self.model = model

    def chat(self, messages: list[dict], *, json_mode: bool = False) -> str:
        if json_mode:
            raise RuntimeError('Toy provider does not generate scientific JSON responses.')
        return (
            f"{self.model} advisor: preserve a wild-type control, quantify replicated "
            "stability, and select a candidate only after protocol verification."
        )

    def chat_with_tools(self, messages: list[dict], tools: list[dict], *, json_mode: bool = False) -> ChatResponse:
        if json_mode:
            raise RuntimeError('Toy provider does not generate scientific JSON responses.')
        text = "\n".join(str(message.get("content") or "") for message in messages)
        task_text = _task_text(messages)
        available = {item["function"]["name"] for item in tools}
        completed = [
            str(message.get("name"))
            for message in messages
            if message.get("role") == "tool"
        ]
        if "skills_list" in available and "skills_list" not in completed:
            return ChatResponse(
                tool_calls=(
                    ToolCall(
                        id="toy-skills-list",
                        name="skills_list",
                        arguments=json.dumps(
                            {"question": _scientific_question(text), "limit": 5}
                        ),
                    ),
                )
            )
        if "skill_view" in available and "skill_view" not in completed:
            return ChatResponse(
                tool_calls=(
                    ToolCall(
                        id="toy-skill-view",
                        name="skill_view",
                        arguments=json.dumps({"name": _selected_skill(messages)}),
                    ),
                )
            )
        sequence = _sequence_from_context(text)
        if (
            sequence
            and "normalize_sequence" in available
            and "normalize_sequence" not in completed
        ):
            return ChatResponse(
                tool_calls=(
                    ToolCall(
                        id="toy-normalize-sequence",
                        name="normalize_sequence",
                        arguments=json.dumps({"sequence": sequence}),
                    ),
                )
            )
        normalized = _normalized_sequence(messages) or sequence
        if (
            normalized
            and _wants_structure(task_text)
            and "resolve_sequence_structure" in available
            and "resolve_sequence_structure" not in completed
        ):
            return ChatResponse(
                tool_calls=(
                    ToolCall(
                        id="toy-resolve-sequence-structure",
                        name="resolve_sequence_structure",
                        arguments=json.dumps(
                            {"sequence": normalized, "reviewed_only": True}
                        ),
                    ),
                )
            )
        if (
            normalized
            and _wants_sequence_analysis(task_text)
            and "analyze_protein_sequence" in available
            and "analyze_protein_sequence" not in completed
        ):
            return ChatResponse(
                tool_calls=(
                    ToolCall(
                        id="toy-analyze-protein-sequence",
                        name="analyze_protein_sequence",
                        arguments=json.dumps(
                            {
                                "sequence": normalized,
                                "sequence_id": "GFP-demo"
                                if "GFP" in text
                                else "query-sequence",
                            }
                        ),
                    ),
                )
            )
        analysis = _tool_observation(messages, "analyze_protein_sequence")
        structure = _tool_observation(messages, "resolve_sequence_structure")
        written_types = {
            observation.get("type")
            for observation in _tool_observations(messages, "write_artifact")
            if isinstance(observation, dict)
        }
        if (
            isinstance(analysis, dict)
            and analysis.get("success") is True
            and "write_artifact" in available
            and "sequence-analysis-json" not in written_types
        ):
            return ChatResponse(
                tool_calls=(
                    ToolCall(
                        id="toy-write-sequence-artifact",
                        name="write_artifact",
                        arguments=json.dumps(
                            {
                                "path": "sequence-analysis/gfp_sequence_analysis.json",
                                "content": json.dumps(
                                    analysis, ensure_ascii=False, indent=2
                                ),
                                "artifact_type": "sequence-analysis-json",
                                "description": (
                                    "Reproducible Biopython protein sequence descriptors"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            )
        if isinstance(structure, dict) and structure.get("success") is True:
            return ChatResponse(
                content=(
                    f"Exact sequence identity resolved to UniProt "
                    f"{structure['uniprot_accession']} "
                    f"({structure.get('protein_name') or 'protein'}; "
                    f"{structure.get('organism') or 'organism unknown'}). "
                    f"Retrieved {structure['structure_source']} structure "
                    f"{structure.get('pdb_id') or 'AlphaFold model'} with "
                    f"{structure.get('atom_records')} ATOM records. The PDB coordinates "
                    "and lookup metadata were registered as verifiable artifacts. "
                    f"Executed tools: {', '.join(completed)}."
                )
            )
        if isinstance(structure, dict) and structure.get("success") is False:
            return ChatResponse(
                content=(
                    "The live sequence-to-structure lookup failed and no structure claim "
                    f"was committed: {structure.get('error')}. Executed tools: "
                    f"{', '.join(completed)}."
                )
            )
        if (
            isinstance(analysis, dict)
            and analysis.get("success") is True
            and "write_artifact" in available
            and "sequence-composition-chart" not in written_types
        ):
            return ChatResponse(
                tool_calls=(
                    ToolCall(
                        id="toy-write-composition-chart",
                        name="write_artifact",
                        arguments=json.dumps(
                            {
                                "path": "sequence-analysis/gfp_amino_acid_composition.svg",
                                "content": _composition_svg(analysis),
                                "artifact_type": "sequence-composition-chart",
                                "description": (
                                    "Amino-acid composition chart calculated from the input sequence"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            )
        candidate = "GFP-B" if "Refine the design" in text else "GFP-A"
        if isinstance(analysis, dict) and analysis.get("success") is True:
            return ChatResponse(
                content=(
                    f"{analysis.get('sequence_id', candidate)} 基础理化性质计算完成："
                    f"长度 {analysis['length_aa']} aa，平均分子量 "
                    f"{analysis['molecular_weight_da']} Da，理论等电点 "
                    f"{analysis['isoelectric_point']}，GRAVY {analysis['gravy']}，"
                    f"不稳定指数 {analysis['instability_index']}。"
                    "完整组成、二级结构倾向、消光系数、方法版本和序列哈希已写入 "
                    "gfp_sequence_analysis.json，并生成氨基酸组成 SVG 图表。"
                    "Executed tools: "
                    f"{', '.join(completed) or 'none'}."
                )
            )
        return ChatResponse(
            content=(
                f"Candidate {candidate}: a toy GFP stability design grounded in the "
                "loaded scientific skill. Executed tools: "
                f"{', '.join(completed) or 'none'}."
            )
        )


def toy_provider_factory(config: LLMConfig) -> ToyChatProvider:
    return ToyChatProvider(config.model)


def _scientific_question(text: str) -> str:
    if _wants_structure(text):
        return "protein structure retrieval UniProt PDB exact sequence"
    if "GFP" in text or _sequence_from_context(text):
        return "protein sequence engineering GFP stability"
    return text[-500:]


def _task_text(messages: list[dict]) -> str:
    first_user = next(
        (str(item.get("content") or "") for item in messages if item.get("role") == "user"),
        "",
    )
    match = re.search(
        r"Scientific task:\s*(.*?)\s*Shared state context:",
        first_user,
        flags=re.DOTALL,
    )
    return match.group(1) if match else first_user


def _selected_skill(messages: list[dict]) -> str:
    all_text = "\n".join(str(item.get("content") or "") for item in messages)
    for message in reversed(messages):
        if message.get("role") != "tool" or message.get("name") != "skills_list":
            continue
        try:
            payload = json.loads(str(message.get("content") or "{}"))
        except json.JSONDecodeError:
            break
        skills = payload.get("skills") or []
        if _wants_structure(all_text):
            for skill in skills:
                if str(skill.get("name")) == "target-structure-retrieval":
                    return "target-structure-retrieval"
        for skill in skills:
            if str(skill.get("name")) == "protein-sequence-engineering":
                return "protein-sequence-engineering"
        if skills:
            return str(skills[0]["name"])
    return "protein-sequence-engineering"


def _sequence_from_context(text: str) -> str | None:
    patterns = (
        r'"sequence"\s*:\s*"([A-Za-z]+)"',
        r"(?:sequence|序列)\s*[:：]\s*([A-Za-z]{3,})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _tool_observation(messages: list[dict], name: str) -> object | None:
    observations = _tool_observations(messages, name)
    return observations[-1] if observations else None


def _tool_observations(messages: list[dict], name: str) -> list[object]:
    observations = []
    for message in messages:
        if message.get("role") != "tool" or message.get("name") != name:
            continue
        content = message.get("content")
        try:
            observations.append(json.loads(str(content)))
        except json.JSONDecodeError:
            observations.append(content)
    return observations


def _normalized_sequence(messages: list[dict]) -> str | None:
    observation = _tool_observation(messages, "normalize_sequence")
    if not isinstance(observation, str):
        return None
    return (
        observation if re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", observation) else None
    )


def _wants_sequence_analysis(text: str) -> bool:
    normalized = text.casefold()
    return any(
        marker in normalized
        for marker in (
            "sequence descriptors",
            "physicochemical",
            "molecular weight",
            "理化性质",
            "分子量",
            "不稳定指数",
        )
    )


def _wants_structure(text: str) -> bool:
    normalized = text.casefold()
    return any(
        marker in normalized
        for marker in (
            "pdb",
            "uniprot",
            "unipro",
            "3d structure",
            "3d结构",
            "三维结构",
            "结构可视化",
        )
    )


def _composition_svg(analysis: dict) -> str:
    counts = analysis.get("amino_acid_counts") or {}
    residues = list("ACDEFGHIKLMNPQRSTVWY")
    maximum = max((int(counts.get(residue, 0)) for residue in residues), default=1)
    bars = []
    for index, residue in enumerate(residues):
        count = int(counts.get(residue, 0))
        height = round(245 * count / maximum, 2)
        x = 62 + index * 40
        y = 325 - height
        bars.append(
            f'<rect x="{x}" y="{y}" width="25" height="{height}" rx="3" '
            'fill="#8f1d22"/>'
            f'<text x="{x + 12.5}" y="345" text-anchor="middle" '
            f'font-size="12">{residue}</text>'
            f'<text x="{x + 12.5}" y="{max(55, y - 7)}" text-anchor="middle" '
            f'font-size="10" fill="#746f67">{count}</text>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="400" '
        'viewBox="0 0 900 400">'
        '<rect width="900" height="400" fill="#fffefa"/>'
        '<text x="40" y="34" font-size="20" font-family="Arial" fill="#272420">'
        "Amino-acid composition · directly counted from sequence</text>"
        '<line x1="45" y1="325" x2="870" y2="325" stroke="#b8ad9d"/>'
        + "".join(bars)
        + f'<text x="40" y="382" font-size="11" fill="#746f67">'
        f"Length {analysis.get('length_aa')} aa · SHA-256 "
        f"{str(analysis.get('sequence_sha256') or '')[:16]}…</text></svg>"
    )
