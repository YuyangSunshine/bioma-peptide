from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE = Path(__file__).parents[1] / "tools/our_tools/geo_datasets.py"


def load_module():
    spec = importlib.util.spec_from_file_location("geo_datasets_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, *, payload=None, text=""):
        self.payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_geo_search_returns_bounded_comparable_series(monkeypatch):
    geo = load_module()

    def fake_get(url, *, params, timeout):
        assert timeout > 0
        if url.endswith("esearch.fcgi"):
            assert '"Homo sapiens"[Organism]' in params["term"]
            assert '"2022/01/01"[PDAT] : "2026/12/31"[PDAT]' in params["term"]
            return Response(payload={"esearchresult": {"idlist": ["1", "2"]}})
        return Response(payload={"result": {
            "uids": ["1", "2"],
            "1": {"accession": "GSE206785", "title": "Gastric single cell", "taxon": "Homo sapiens", "n_samples": "24", "PDAT": "2022"},
            "2": {"accession": "GPL1", "title": "Platform"},
        }})

    monkeypatch.setattr("requests.get", fake_get)
    result = geo.search_geo_datasets("gastric cancer single cell", "Homo sapiens", 2022, 2026, 5)
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["accession"] == "GSE206785"
    assert result["candidates"][0]["sample_count"] == 24
    assert geo.validate_search(result, {"query": "gastric cancer single cell", "limit": 5})


def test_geo_inspection_returns_official_https_manifest(monkeypatch):
    geo = load_module()

    def fake_get(url, *, params, timeout):
        if url.endswith("esearch.fcgi"):
            return Response(payload={"esearchresult": {"idlist": ["7"]}})
        if url.endswith("esummary.fcgi"):
            return Response(payload={"result": {"uids": ["7"], "7": {
                "accession": "GSE206785", "title": "Gastric single cell",
                "taxon": "Homo sapiens", "n_samples": "24",
            }}})
        return Response(text="\n".join([
            "!Series_title = Gastric single cell",
            "!Series_supplementary_file = ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE206nnn/GSE206785/suppl/GSE206785_metadata.txt.gz",
            "!Series_supplementary_file_2 = https://ftp.ncbi.nlm.nih.gov/geo/series/GSE206nnn/GSE206785/suppl/GSE206785_scgex.txt.gz",
        ]))

    monkeypatch.setattr("requests.get", fake_get)
    result = geo.inspect_geo_dataset("gse206785")
    assert result["dataset"]["accession"] == "GSE206785"
    assert result["resource_count"] == 2
    assert all(item["url"].startswith("https://ftp.ncbi.nlm.nih.gov/") for item in result["resources"])
    assert all(item["role"] == "supplementary_file_unclassified" for item in result["resources"])
    assert geo.validate_inspection(result, {"accession": "GSE206785"})


def test_geo_accession_and_year_range_are_rejected_before_network():
    geo = load_module()
    with pytest.raises(ValueError, match="exact GEO Series"):
        geo.GEOInspectInput(accession="https://example.org/GSE1")
    with pytest.raises(ValueError, match="start_year"):
        geo.search_geo_datasets("heart RNA", start_year=2026, end_year=2022)


def test_geo_tools_publish_distinct_search_and_inspection_contracts():
    geo = load_module()
    search = geo.search_geo_datasets._bioma_tool
    inspect = geo.inspect_geo_dataset._bioma_tool
    assert search.name == "search_geo_datasets"
    assert inspect.name == "inspect_geo_dataset"
    assert search.result_kind == inspect.result_kind == "retrieved_fact"
    assert search.requires_network and inspect.requires_network
    assert "accession" in inspect.parameters_schema()["properties"]
