"""Execute ESM operations inside the isolated ESM Python environment."""

from __future__ import annotations

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys


def _validate_sequences(sequences: list[str], allow_mask: bool = False) -> list[str]:
    if not sequences or len(sequences) > 32:
        raise ValueError("sequences must contain between 1 and 32 entries")
    alphabet = set("ACDEFGHIKLMNPQRSTVWY") | ({"_"} if allow_mask else set())
    cleaned = [sequence.strip().upper() for sequence in sequences]
    for sequence in cleaned:
        invalid = sorted(set(sequence) - alphabet)
        if not sequence or invalid:
            raise ValueError(f"Invalid protein sequence characters: {''.join(invalid)}")
        if len(sequence) > 4096:
            raise ValueError("Sequence length is limited to 4,096 residues")
    return cleaned


def run(payload: dict[str, object]) -> dict[str, object]:
    from esm.sdk.api import ESMProtein, GenerationConfig, LogitsConfig

    operation = str(payload["operation"])
    values = list(payload.get("sequences") or [])
    output = Path(str(payload["output_path"])).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    model_name = str(payload.get("model_name") or "")
    num_steps = int(payload.get("num_steps") or 16)
    temperature = float(payload.get("temperature") or 0.7)
    if operation == "embeddings":
        import numpy as np
        from esm.models.esmc import ESMC

        cleaned = _validate_sequences(values)
        if output.suffix.lower() != ".npy":
            output = output.with_suffix(".npy")
        model = ESMC.from_pretrained(model_name or "esmc_600m")
        pooled = []
        for sequence in cleaned:
            encoded = model.encode(ESMProtein(sequence=sequence))
            result = model.logits(encoded, LogitsConfig(return_embeddings=True))
            embeddings = result.embeddings
            if embeddings.ndim == 3:
                embeddings = embeddings[0]
            if embeddings.shape[0] >= len(sequence) + 2:
                embeddings = embeddings[1:len(sequence) + 1]
            pooled.append(embeddings.mean(dim=0).detach().cpu().numpy())
        matrix = np.stack(pooled)
        np.save(output, matrix)
        return {"operation": operation, "output": str(output), "shape": list(matrix.shape)}

    from esm.models.esm3 import ESM3

    model = ESM3.from_pretrained(model_name or "esm3_sm_open_v1")
    if operation == "generate_sequence":
        cleaned = _validate_sequences(values, allow_mask=True)
        if len(cleaned) != 1:
            raise ValueError("generate_sequence requires exactly one masked prompt")
        result = model.generate(
            ESMProtein(sequence=cleaned[0]),
            GenerationConfig(track="sequence", num_steps=num_steps, temperature=temperature),
        )
        output.write_text(f">esm3_generated\n{result.sequence}\n", encoding="utf-8")
        return {"operation": operation, "output": str(output), "length": len(result.sequence)}
    if operation == "predict_structure":
        cleaned = _validate_sequences(values)
        if len(cleaned) != 1:
            raise ValueError("predict_structure requires exactly one sequence")
        result = model.generate(
            ESMProtein(sequence=cleaned[0]),
            GenerationConfig(track="structure", num_steps=num_steps),
        )
        result.to_pdb(str(output))
        return {"operation": operation, "output": str(output), "length": len(cleaned[0])}

    from esm.utils.structure.protein_chain import ProteinChain

    structure = Path(str(payload.get("pdb_path") or "")).expanduser().resolve()
    if not structure.is_file():
        raise FileNotFoundError("pdb_path is required for inverse_fold")
    chain = ProteinChain.from_pdb(str(structure))
    prompt = ESMProtein.from_protein_chain(chain)
    prompt.sequence = None
    designs = []
    for _ in range(int(payload.get("num_designs") or 1)):
        result = model.generate(
            prompt,
            GenerationConfig(track="sequence", num_steps=num_steps, temperature=temperature),
        )
        designs.append(result.sequence)
    output.write_text(
        "".join(f">esm3_design_{index}\n{sequence}\n" for index, sequence in enumerate(designs, 1)),
        encoding="utf-8",
    )
    return {"operation": operation, "output": str(output), "designs": len(designs)}


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    with redirect_stdout(sys.stderr):
        result = run(payload)
    print(json.dumps(result, ensure_ascii=False))
