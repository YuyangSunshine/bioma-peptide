"""R process execution with the same delivery contract as Python analysis.

The shared process boundary supplies filesystem, network and process isolation
when invoked by the scientific harness. The host validates files and records
the process result separately from the program's scientific claims.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from bioma.harness.types import ToolObservation
from bioma.runtime.deadline import check, remaining


def rscript_path():
    configured = os.getenv("BIOMA_RSCRIPT")
    if configured:
        return str(Path(configured).resolve()) if Path(configured).is_file() else None
    located = shutil.which("Rscript")
    if located:
        return located
    local = Path.cwd() / '.bioma' / 'toolchains' / 'R' / 'bin' / 'Rscript.exe'
    return str(local.resolve()) if local.is_file() else None


def r_environment():
    executable = rscript_path()
    return {
        "engine": "R",
        "executable": executable,
        "available": executable is not None,
    }


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execute_r_analysis(context, args):
    from bioma.harness.code_analysis import (
        AnalysisReport,
        _declared_outputs,
        resolve_analysis_inputs,
    )
    from bioma.harness.output_writer import render_report

    executable = rscript_path()
    if executable is None:
        raise ValueError(
            "R runtime unavailable: configure BIOMA_RSCRIPT or install Rscript on PATH. Python substitution is prohibited."
        )
    reports = {
        name: AnalysisReport.model_validate(value)
        for name, value in args.get("reports", {}).items()
    }
    names = _declared_outputs(context, args["output_files"], reports)
    reuse_catalog, selected_inputs = resolve_analysis_inputs(context, args)
    inputs = [str(Path(p).resolve()) for p in selected_inputs]
    if not inputs or not all(Path(p).is_file() for p in inputs):
        raise ValueError("R analysis requires existing input files.")
    inputs_digest = {p: _digest(p) for p in inputs}
    folder = context.artifact_dir("code_analysis", uuid4().hex[:12]).resolve()
    output = folder / "outputs"
    output.mkdir()
    from bioma.harness.output_writer import (stage_reused_inputs, verify_reused_inputs,
        export_paired_report, build_artifact_dispositions)
    script, driver = folder / "analysis.R", folder / "run.R"
    published, receipt_path = folder / "published.json", folder / "receipt.json"
    parameters = folder / "inputs.json"
    parameters.write_text(
        json.dumps(
            {
                "inputs": inputs,
                "output": str(output),
                "request": context.state.get("current_user_request", ""),
                "published": str(published),
                "script": str(script),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    script.write_text(args["code"], encoding="utf-8", newline="\n")
    # Paths are read as data, never interpolated as executable R source.
    driver.write_text(
        """if (!requireNamespace("jsonlite", quietly=TRUE)) stop("Required R package unavailable: jsonlite")
local({
  parameters <- jsonlite::fromJSON(commandArgs(trailingOnly=TRUE)[1])
  list2env(list(INPUT_FILES=parameters$inputs, OUTPUT_DIR=parameters$output,
               REQUEST=parameters$request), envir=.GlobalEnv)
  assign("write_json", local({
    destination <- parameters$output
    function(filename, data) {
      jsonlite::write_json(data, file.path(destination, filename), auto_unbox=TRUE, digits=NA, na="null")
    }
  }), envir=.GlobalEnv)
  assign("publish_result", local({
    destination <- parameters$published
    function(summary) {
      if (!is.list(summary) || is.null(names(summary))) stop("publish_result requires a named list")
      if (file.exists(destination)) stop("Publish the final result only once")
      jsonlite::write_json(list(summary=summary), destination, auto_unbox=TRUE, digits=NA, na="null")
    }
  }), envir=.GlobalEnv)
  for (binding in c("INPUT_FILES", "OUTPUT_DIR", "REQUEST", "write_json", "publish_result")) lockBinding(binding, .GlobalEnv)
  source(parameters$script, local=.GlobalEnv, encoding="UTF-8")
  writeLines(capture.output(sessionInfo()), file.path(dirname(parameters$script), "sessionInfo.txt"))
})
""",
        encoding="utf-8",
        newline="\n",
    )
    check()
    budget = remaining()
    from bioma.backends.analysis.processes import code_action_boundary, run_python
    with code_action_boundary(folder, inputs):
        result = run_python(driver, executable=executable, arguments=(str(parameters),),
                            cwd=output, timeout=budget if budget is not None else 180)
    (folder / 'stdout.txt').write_text(result.stdout or '', encoding='utf-8')
    (folder / 'stderr.txt').write_text(result.stderr or '', encoding='utf-8')
    process = {
        "engine": "R",
        "executable": executable,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "script_sha256": _digest(script),
        "input_sha256": inputs_digest,
    }
    receipt_path.write_text(
        json.dumps({"process": process}, ensure_ascii=False), encoding="utf-8"
    )
    check()
    if result.returncode:
        from bioma.harness.recovery import FailedAnalysisExecution
        raise FailedAnalysisExecution(
            f"R process failed (receipt: {receipt_path}): {result.stderr[-6000:]}", receipt_path, names
        )
    if not published.is_file():
        if not context.state.get('investigation_intermediate') or reports:
            raise ValueError(f"Missing R analysis result: call publish_result(summary). Diagnostics: {folder / 'stdout.txt'}")
        receipt = {'execution_status': 'succeeded', 'summary_status': 'not_published', 'summary': None}
    else:
        receipt = json.loads(published.read_text(encoding="utf-8"))
        if not isinstance(receipt.get("summary"), dict):
            raise ValueError("R summary must be a named object.")
        receipt.update(execution_status='succeeded', summary_status='published')
    reused = stage_reused_inputs(context, args, output, reuse_catalog)
    paths = []
    for name in names:
        path = (output / name).resolve()
        path.relative_to(output)
        if path.suffix.lower() == ".md":
            if path.exists():
                raise ValueError(
                    "Declare Markdown layout; do not write reports in R code."
                )
        elif path.suffix.lower()=='.pdf' and str(Path(name).with_suffix('.md')) in reports and not path.exists():
            pass
        else:
            if not path.is_file() or not path.stat().st_size:
                raise ValueError(f"Missing or empty declared R output: {name}")
            if path.suffix.lower() == ".json":
                json.loads(path.read_text(encoding="utf-8-sig"))
            elif path.suffix.lower() == ".png":
                from PIL import Image

                with Image.open(path) as img:
                    img.verify()
            elif path.suffix.lower() == ".pdf":
                from pypdf import PdfReader

                if not PdfReader(path).pages:
                    raise ValueError(f"Empty PDF: {name}")
            elif path.suffix.lower() == ".svg":
                from xml.etree import ElementTree

                if ElementTree.parse(path).getroot().tag.split("}")[-1] != "svg":
                    raise ValueError(f"Invalid SVG: {name}")
            elif path.suffix.lower() in {'.pdb', '.cif', '.mmcif'}:
                from bioma.harness.code_analysis import validate_structure_file
                validate_structure_file(path)
        paths.append(str(path))
    if any(_digest(p) != digest for p, digest in inputs_digest.items()):
        raise ValueError("R analysis changed input files; delivery rejected.")
    runtime = {
        **r_environment(),
        "session_info": (folder / "sessionInfo.txt").read_text(encoding="utf-8"),
    }
    verify_reused_inputs(reused)
    provenance = {
        'reused_inputs':reused,
        "input_versions": [
            {"file": Path(p).name, "sha256": h} for p, h in inputs_digest.items()
        ],
        "execution_environment": runtime,
    }
    try:
        for name, report in reports.items():
            render_report(
                output,
                name,
                title=report.title,
                summary=receipt["summary"],
                sources=report.sources,
                provenance=provenance,
                methods=report.methods,
                limitations=report.limitations,
                introduction=report.introduction,
                conclusions=report.conclusions,
            )
            export_paired_report(output, name, names)
    except (ValueError, OSError) as exc:
        from bioma.harness.recovery import FailedDeliveryRendering
        raise FailedDeliveryRendering(str(exc),receipt_path,names) from exc
    provenance['artifact_dispositions'] = build_artifact_dispositions(
        args, source_catalog=reuse_catalog, output_dir=output,
        output_paths=paths, reused_records=reused)
    receipt.update(process=process, provenance=provenance)
    from bioma.runtime.tool_actions import current_tool_action
    receipt['dispatch'] = current_tool_action()
    receipt['execution_arguments'] = {key:value for key,value in args.items() if key != 'code'}
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    context.state["code_validation_stage"] = 3
    return ToolObservation(
        tool_name="execute_analysis_code",
        status="ok",
        summary="Executed R and verified declared artifacts; consistency review pending.",
        artifacts=paths,
        metrics={
            "summary": receipt["summary"],
            "summary_status": receipt["summary_status"],
            "diagnostic_stdout": str(folder / "stdout.txt"),
            "sha256": {p: _digest(p) for p in paths},
            "input_sha256": inputs_digest,
            "runtime": runtime,
            "receipt_path": str(receipt_path),
            "execution_sha256": {str(p):_digest(p) for p in (receipt_path, script, driver)},
        },
        patch={
            "analysis_results": {
                "findings": ([{"summary": receipt["summary"]}] if receipt["summary_status"] == "published" else []),
                "analysis_outputs": {"code_analysis": paths},
            },
            "analysis_code_paths": [str(script), str(driver)],
            "execution_receipts": [str(receipt_path)],
            "visualizations": [
                {"path": p}
                for p in paths
                if Path(p).suffix.lower() in {".png", ".svg", ".pdf"}
            ],
            "final_report": next(
                (p for p in paths if Path(p).suffix.lower() == ".md"), ""
            ),
            "error_code": None,
            "error_detail": None,
        },
    )
