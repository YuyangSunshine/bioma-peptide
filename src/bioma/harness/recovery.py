"""Preserve failed-process files as unqualified inputs, never completed results."""
import hashlib
import json
from pathlib import Path


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''):h.update(block)
    return h.hexdigest()


class FailedAnalysisExecution(ValueError):
    def __init__(self, message, receipt_path, declared_outputs):
        super().__init__(message)
        self.receipt_path=str(receipt_path)
        self.declared_outputs=list(declared_outputs)
        self.retained_outputs=None


class FailedDeliveryRendering(FailedAnalysisExecution):
    """The process succeeded but its report has not passed delivery checks."""


def retain_partial_outputs(context, failure):
    receipt=Path(failure.receipt_path).resolve()
    receipt.relative_to(context.artifact_dir('code_analysis').resolve())
    process=json.loads(receipt.read_text())['process']
    engine=process.get('engine')
    script=receipt.parent/('analysis.R' if engine=='R' else 'analysis.py')
    postprocessing=isinstance(failure,FailedDeliveryRendering)
    valid_exit=(process.get('exit_code')==0 if postprocessing else bool(process.get('exit_code')))
    if engine not in {'R','python'} or not valid_exit or sha(script)!=process['script_sha256']:
        raise ValueError('Partial-output recovery requires an actual failed Python/R process receipt')
    inputs={str(Path(p).resolve()):sha(p) for p in context.state.get('raw_data_sources',[])}
    if inputs!=process.get('input_sha256'):
        raise ValueError('Failed execution input scope changed')
    output=(receipt.parent/'outputs').resolve()
    files={}
    for name in failure.declared_outputs:
        path=output/name
        resolved=path.resolve()
        resolved.relative_to(output)
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p!=output and p.is_relative_to(output)):
            raise ValueError('Partial outputs cannot be symbolic links')
        if resolved.is_file() and resolved.stat().st_size:files[str(resolved)]=sha(resolved)
    if not files:return None
    manifest=receipt.parent/'partial_outputs.json'
    data={'kind':'incomplete_delivery_outputs' if postprocessing else 'failed_execution_partial_outputs','scientific_status':'unqualified',
        'failure_stage':'report_rendering' if postprocessing else 'process_execution','failure_reason':str(failure),
        'receipt_path':str(receipt),'receipt_sha256':sha(receipt),
        'source_path':str(script),'source_sha256':sha(script),'input_sha256':inputs,'files':files,
        'scope':'Files survived an incomplete action. Process status and failure stage are separate; files are not validated delivery. Inspect/reuse in a new successful computation before review.'}
    manifest.write_text(json.dumps(data,sort_keys=True))
    bindings=dict(context.state.get('admitted_execution_inputs',{}))
    for path,digest in files.items():
        bindings[path]={'sha256':digest,'manifest_path':str(manifest),'manifest_sha256':sha(manifest)}
    context.state['admitted_execution_inputs']=bindings
    failure.retained_outputs={'manifest_path':str(manifest),'files':files,
        'relative_files':{str(Path(p).relative_to(output)):h for p,h in files.items()}, 'scope':data['scope']}
    return failure.retained_outputs


def verify_partial_inputs(bindings):
    provenance={}
    for path,binding in bindings.items():
        manifest=Path(binding['manifest_path'])
        if sha(manifest)!=binding['manifest_sha256']:
            raise ValueError('Partial execution manifest changed')
        data=json.loads(manifest.read_text())
        if data.get('kind') not in {'failed_execution_partial_outputs','incomplete_delivery_outputs'} or data.get('files',{}).get(path)!=binding['sha256']:
            raise ValueError('Partial execution input is not bound to its manifest')
        expected={str(manifest):binding['manifest_sha256'], data['receipt_path']:data['receipt_sha256'],
                  data['source_path']:data['source_sha256'],path:binding['sha256']}
        for filename,digest in {**expected, **data['input_sha256']}.items():
            if sha(filename)!=digest:raise ValueError('Partial execution provenance or input changed')
        provenance.update(expected)
    return provenance
