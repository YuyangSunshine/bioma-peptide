"""Observational S1-N metadata. This module never imports runtime registries.

This index is deliberately separate from capabilities.CapabilityRegistry, which
participates in routing. The bundled v1 catalog is a frozen, historical,
read-only metadata snapshot, not an executable or qualified capability registry.
Returned records are detached copies, not live state.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"
VALIDATION_DIMENSIONS = (
    "structural", "binding", "load", "execution", "output", "scientific",
    "benchmark", "deployment",
)
VALIDATION_STATES = frozenset({"PASS", "FAIL", "UNKNOWN", "NOT_RUN", "NOT_APPLICABLE", "BLOCKED"})
CONFIDENCES = frozenset({"UNKNOWN", "LOW", "MEDIUM", "HIGH", "EXACT"})
ASSET_TYPES = frozenset({
    "COMPUTATIONAL_METHOD", "VISUALIZATION", "DATA_SOURCE", "EXTERNAL_SERVICE",
    "MODEL_CAPABILITY", "TOOL_CAPABILITY", "WORKFLOW", "SCIENTIFIC_GUIDANCE", "UNKNOWN",
})
RESOURCE_TYPES = frozenset({"TOOL_BACKEND", "MODEL_BACKEND", "EXTERNAL_SERVICE", "DEVICE", "WORKFLOW", "SCIENTIFIC_GUIDANCE"})
DISPOSITIONS = frozenset({
    "KEEP_CANONICAL", "KEEP_PROVIDER_VARIANT", "KEEP_WORKFLOW", "KEEP_GUIDANCE",
    "ALIAS_TO", "MERGE_RECIPE_CANDIDATE", "REPAIR_REQUIRED", "DEPRECATE_CANDIDATE",
    "QUARANTINE_CANDIDATE", "HUMAN_REVIEW",
})


class CapabilityIndexError(ValueError):
    """Missing, malformed, unsupported or internally inconsistent sidecar."""


class AmbiguousLegacySkillError(CapabilityIndexError):
    """An identifier matches several legacy records; no guess is made."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CapabilityIndexError(message)


def _object(value: Any, label: str) -> dict:
    _require(isinstance(value, dict), f"{label}: expected object")
    return value


def _fields(value: dict, fields: tuple[str, ...], label: str) -> None:
    _require(all(field in value for field in fields), f"{label}: missing required fields {fields}")


def _text(value: Any, label: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()), f"{label}: expected nonempty string")


def _strings(value: Any, label: str) -> None:
    _require(isinstance(value, list), f"{label}: expected string array")
    for item in value:
        _text(item, label)
    _require(len(value) == len(set(value)), f"{label}: duplicate array item")


def _states(value: Any, label: str, *, summary: bool = False) -> None:
    value = _object(value, label)
    _require(set(value) == set(VALIDATION_DIMENSIONS), f"{label}: expected eight validation dimensions")
    for dimension, state in value.items():
        if summary:
            state = _object(state, label + "." + dimension)
            _require(bool(state) and set(state) <= VALIDATION_STATES, f"{label}: invalid validation state")
            _require(all(type(count) is int and count >= 0 for count in state.values()), f"{label}: invalid state count")
        else:
            _require(isinstance(state, str) and state in VALIDATION_STATES, f"{label}.{dimension}: invalid state")


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key {key!r}")
        result[key] = value
    return result


class CapabilityIndex:
    """Load once; query by exact IDs/paths without aliases or runtime routing.

    `root` is a sidecar directory. Defaults to resources/capabilities/v1 beside
    this module in a source checkout or normally installed wheel. Missing IDs
    return None (or an empty tuple for collections). Ambiguous legacy identities
    raise an error. No suffix matching, name fallback, or alias collapse occurs.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        root = Path(root) if root is not None else Path(__file__).parent / "resources" / "capabilities" / "v1"
        documents = {}
        for name in (
            "schema_version", "capability_catalog", "skill_capability_map",
            "backend_catalog", "data_source_catalog", "aliases",
        ):
            path = root / (name + ".json")
            try:
                value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs)
            except (OSError, UnicodeError, ValueError) as exc:
                raise CapabilityIndexError(f"{path.name}: {exc}") from exc
            documents[name] = _object(value, path.name)
            _require(value.get("schema_version") == SCHEMA_VERSION, f"{path.name}: unsupported schema_version")
        schema = documents["schema_version"]
        _require(schema.get("runtime_integration") == "NONE", "schema_version: runtime integration must be NONE")
        _require(schema.get("alias_policy") == "METADATA_ONLY_NO_COLLAPSE", "schema_version: aliases must be metadata only")
        for name, document in documents.items():
            _require(document.get("source") == schema.get("source") and isinstance(document.get("source"), dict),
                     f"{name}: inconsistent source snapshot")
        self._capabilities = self._records(documents["capability_catalog"], "capabilities", "capability_id")
        self._skills = self._records(documents["skill_capability_map"], "skills", "stable_skill_id")
        self._implementations = self._records(documents["backend_catalog"], "implementations", "implementation_id")
        self._resources = self._records(documents["backend_catalog"], "resources", "resource_id")
        self._data_sources = self._records(documents["data_source_catalog"], "data_sources", "resource_id")
        _require(not set(self._resources) & set(self._data_sources), "duplicate resource ID across catalogs")
        self._aliases = self._records(documents["aliases"], "aliases", "source_stable_skill_id")
        self._legacy: dict[str, set[str]] = {}
        self._paths: dict[str, str] = {}
        try:
            self._validate()
        except (KeyError, TypeError, AttributeError) as exc:
            raise CapabilityIndexError(f"Malformed sidecar record: {exc}") from exc
        self._metadata = deepcopy(schema)

    @staticmethod
    def _records(document: dict, collection: str, identity: str) -> dict:
        records = document.get(collection)
        _require(isinstance(records, list), f"{collection}: expected array")
        result = {}
        for record in records:
            record = _object(record, collection)
            _text(record.get(identity), collection + "." + identity)
            key = record[identity]
            _require(key not in result, f"duplicate {identity}: {key}")
            result[key] = record
        return result

    def _references(self, values: Any, target: dict, label: str) -> None:
        _strings(values, label)
        _require(set(values) <= set(target), f"{label}: dangling reference")

    def _validate(self) -> None:
        all_resources = {**self._resources, **self._data_sources}
        for cid, cap in self._capabilities.items():
            _fields(cap, ("canonical_name", "definition", "domains", "function", "scientific_objects",
                          "input_types", "output_types", "implementation_refs", "validation_summary",
                          "normalization_status", "normalization_confidence", "source_skill_ids"), cid)
            for field in ("canonical_name", "definition", "function"):
                _text(cap[field], cid + "." + field)
            for field in ("domains", "scientific_objects", "input_types", "output_types"):
                _strings(cap[field], cid + "." + field)
            _require(cap["normalization_status"] in {"PROPOSED", "PROVISIONAL"}, f"{cid}: invalid normalization status")
            _require(cap["normalization_confidence"] in CONFIDENCES, f"{cid}: invalid confidence")
            _states(cap["validation_summary"], cid, summary=True)
            self._references(cap["implementation_refs"], self._implementations, cid + ".implementation_refs")
            self._references(cap["source_skill_ids"], self._skills, cid + ".source_skill_ids")
            if cap["normalization_status"] == "PROVISIONAL":
                _require(cap["normalization_confidence"] == "UNKNOWN", f"{cid}: provisional confidence must remain UNKNOWN")
        for sid, skill in self._skills.items():
            _fields(skill, ("legacy_path", "legacy_skill_id", "capability_ids", "implementation_refs", "resource_refs",
                            "mapping_confidence", "normalization_status", "primary_asset_type", "proposed_disposition",
                            "validation", "validation_refs", "provenance_refs"), sid)
            _text(skill["legacy_path"], sid + ".legacy_path")
            _text(skill["legacy_skill_id"], sid + ".legacy_skill_id")
            _require(skill["legacy_path"].replace("\\", "/") == skill["legacy_path"], f"{sid}: legacy_path must use forward slashes")
            _require(skill["legacy_path"] not in self._paths, f"duplicate legacy_path: {skill['legacy_path']}")
            self._paths[skill["legacy_path"]] = sid
            self._legacy.setdefault(skill["legacy_skill_id"], set()).add(sid)
            self._references(skill["capability_ids"], self._capabilities, sid + ".capability_ids")
            self._references(skill["implementation_refs"], self._implementations, sid + ".implementation_refs")
            self._references(skill["resource_refs"], all_resources, sid + ".resource_refs")
            for field in ("validation_refs", "provenance_refs"):
                _strings(skill[field], sid + "." + field)
            _require(skill["mapping_confidence"] in CONFIDENCES, f"{sid}: invalid confidence")
            _require(skill["primary_asset_type"] in ASSET_TYPES, f"{sid}: invalid primary asset type")
            _require(skill["proposed_disposition"] in DISPOSITIONS, f"{sid}: invalid disposition")
            _require(skill["normalization_status"] in {"PROPOSED", "PROVISIONAL"}, f"{sid}: invalid normalization status")
            if skill["normalization_status"] == "PROVISIONAL":
                _require(skill["mapping_confidence"] == "UNKNOWN", f"{sid}: provisional confidence must remain UNKNOWN")
            _states(skill["validation"], sid)
        for iid, impl in self._implementations.items():
            _fields(impl, ("type", "provider", "backend_refs", "capability_ids", "source_skill_ids", "execution_mode",
                           "dependencies", "version", "validation_refs", "validation", "source_ref"), iid)
            _require(impl["type"] == "TOOL_IMPLEMENTATION", f"{iid}: invalid implementation type")
            _text(impl["provider"], iid + ".provider")
            _text(impl["execution_mode"], iid + ".execution_mode")
            _text(impl["version"], iid + ".version")
            self._references(impl["backend_refs"], all_resources, iid + ".backend_refs")
            self._references(impl["capability_ids"], self._capabilities, iid + ".capability_ids")
            self._references(impl["source_skill_ids"], self._skills, iid + ".source_skill_ids")
            _strings(impl["dependencies"], iid + ".dependencies")
            _strings(impl["validation_refs"], iid + ".validation_refs")
            _states(impl["validation"], iid)
            _object(impl["source_ref"], iid + ".source_ref")
        for rid, resource in all_resources.items():
            _fields(resource, ("type", "name", "source_skill_ids", "capability_ids", "evidence_refs", "normalization_confidence"), rid)
            allowed = {"DATA_SOURCE"} if rid in self._data_sources else RESOURCE_TYPES
            _require(resource["type"] in allowed, f"{rid}: invalid resource type")
            _text(resource["name"], rid + ".name")
            _require(resource["normalization_confidence"] in CONFIDENCES, f"{rid}: invalid resource confidence")
            self._references(resource["source_skill_ids"], self._skills, rid + ".source_skill_ids")
            self._references(resource["capability_ids"], self._capabilities, rid + ".capability_ids")
            _strings(resource["evidence_refs"], rid + ".evidence_refs")
        for cid, cap in self._capabilities.items():
            for sid in cap["source_skill_ids"]:
                _require(cid in self._skills[sid]["capability_ids"], f"{cid}/{sid}: inconsistent capability membership")
            for counts in cap["validation_summary"].values():
                _require(sum(counts.values()) == len(cap["source_skill_ids"]), f"{cid}: inconsistent validation count")
            for iid in cap["implementation_refs"]:
                _require(cid in self._implementations[iid]["capability_ids"], f"{cid}/{iid}: inconsistent implementation edge")
        for iid, impl in self._implementations.items():
            for cid in impl["capability_ids"]:
                _require(iid in self._capabilities[cid]["implementation_refs"], f"{iid}/{cid}: inconsistent capability edge")
            for sid in impl["source_skill_ids"]:
                _require(iid in self._skills[sid]["implementation_refs"], f"{iid}/{sid}: inconsistent skill edge")
        for sid, alias in self._aliases.items():
            _fields(alias, ("target_stable_skill_id", "legacy_skill_id", "legacy_path", "classification", "policy"), sid)
            target = alias["target_stable_skill_id"]
            _require(sid in self._skills and isinstance(target, str) and target in self._skills and target != sid, f"{sid}: invalid alias target")
            _require(alias["policy"] == "METADATA_ONLY_NO_COLLAPSE", f"{sid}: invalid alias policy")
            _require(alias["classification"] == "BYTE_IDENTICAL_DUPLICATE", f"{sid}: unsupported alias evidence")
            _require(alias["legacy_skill_id"] == self._skills[sid]["legacy_skill_id"] and alias["legacy_path"] == self._skills[sid]["legacy_path"], f"{sid}: inconsistent alias legacy identity")
            _require(self._skills[sid]["proposed_disposition"] == "ALIAS_TO", f"{sid}: inconsistent alias disposition")
            seen = {sid}
            while target in self._aliases:
                _require(target not in seen, f"{sid}: alias cycle")
                seen.add(target)
                target = self._aliases[target]["target_stable_skill_id"]
        for sid, skill in self._skills.items():
            for cid in skill["capability_ids"]:
                _require(sid in self._capabilities[cid]["source_skill_ids"], f"{sid}/{cid}: inconsistent skill membership")
            for iid in skill["implementation_refs"]:
                _require(sid in self._implementations[iid]["source_skill_ids"], f"{sid}/{iid}: inconsistent implementation membership")
            _require((skill["proposed_disposition"] == "ALIAS_TO") == (sid in self._aliases), f"{sid}: missing/unexpected alias record")

    @property
    def metadata(self) -> dict:
        return deepcopy(self._metadata)

    def get_capability(self, capability_id: str) -> dict | None:
        return deepcopy(self._capabilities.get(capability_id))

    def get_skill_mapping(self, skill_id_or_path: str | Path) -> dict | None:
        key = str(skill_id_or_path).replace("\\", "/")
        matches = set(self._legacy.get(key, ()))
        if key in self._skills:
            matches.add(key)
        if key in self._paths:
            matches.add(self._paths[key])
        if len(matches) > 1:
            raise AmbiguousLegacySkillError(f"Ambiguous legacy skill identity {key!r}; use exact stable ID or path")
        return deepcopy(self._skills[next(iter(matches))]) if matches else None

    def resolve_legacy_skill(self, skill_id_or_path: str | Path) -> dict | None:
        """Return the original mapping, even for an alias proposal."""
        return self.get_skill_mapping(skill_id_or_path)

    def find_capabilities(self, *, domain: str | None = None, function: str | None = None,
                          object: str | None = None) -> tuple[dict, ...]:
        return tuple(deepcopy(cap) for _, cap in sorted(self._capabilities.items())
                     if (domain is None or domain in cap["domains"])
                     and (function is None or function == cap["function"])
                     and (object is None or object in cap["scientific_objects"]))

    def get_implementations(self, capability_id: str) -> tuple[dict, ...]:
        cap = self._capabilities.get(capability_id)
        return tuple(deepcopy(self._implementations[iid]) for iid in sorted(cap["implementation_refs"])) if cap else ()

    def get_alias_proposal(self, skill_id_or_path: str | Path) -> dict | None:
        mapping = self.get_skill_mapping(skill_id_or_path)
        return deepcopy(self._aliases.get(mapping["stable_skill_id"])) if mapping else None

    def get_resource(self, resource_id: str) -> dict | None:
        return deepcopy(self._resources.get(resource_id, self._data_sources.get(resource_id)))

    def list_skill_mappings(self) -> tuple[dict, ...]:
        return tuple(deepcopy(record) for _, record in sorted(self._skills.items()))
