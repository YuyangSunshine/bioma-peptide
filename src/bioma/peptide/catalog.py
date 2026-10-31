"""Inspect the ten real decorators without importing the legacy runtime."""
import ast
from pathlib import Path
from .contracts import TOOLS, sha256


def catalog(repo):
    rows = []
    for name, module in TOOLS.items():
        relative = ("tools/our_tools/tbmb_bicyclic_docking.py" if module == "tbmb_bicyclic_docking"
                    else f"tools/our_tools/_crosslinked_impl/{module}.py")
        path = Path(repo) / relative
        tree = ast.parse(path.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        deco = next(n for n in fn.decorator_list if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "bioma_tool")
        meta = {k.arg: ast.unparse(k.value) for k in deco.keywords}
        rows.append({"name": name, "source": relative, "source_sha256": sha256(path),
                     "version": ast.literal_eval(next(k.value for k in deco.keywords if k.arg == "version")),
                     "legacy_args_schema": meta.get("args_schema"),
                     "parameters": ast.unparse(fn.args), "result_kind": "computed_prediction",
                     "result_validator": "bioma.peptide.contracts.result_validator",
                     "authorization": "registered_profile_only", "legacy_metadata": meta})
    return rows


def source_files(repo):
    files={r['source'] for r in catalog(repo)} | {'src/bioma/tools/base.py','src/bioma/tools/errors.py','tools/our_tools/crosslinked_peptide_structure.py'}
    contract='tools/our_tools/_crosslinked_impl/ensemble_contract.py'
    if (Path(repo)/contract).exists():
        files.add(contract)
    return files
