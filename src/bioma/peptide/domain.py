"""Execute hash-pinned domain methods from the native BioMA package.

Only trusted, hash-pinned profiles are accepted. This is not a generic Python or
shell executor. The minimal module namespace is private to the child process.
"""
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import shutil
from .contracts import TOOLS, sha256, result_validator


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_tools(repo, source_hashes):
    repo = Path(repo).resolve(strict=True)
    required = {'src/bioma/tools/base.py', 'src/bioma/tools/errors.py',
                'tools/our_tools/crosslinked_peptide_structure.py',
                'tools/our_tools/tbmb_bicyclic_docking.py'}
    required |= {f'tools/our_tools/_crosslinked_impl/{x}.py' for x in set(TOOLS.values()) if x != 'tbmb_bicyclic_docking'}
    contract = 'tools/our_tools/_crosslinked_impl/ensemble_contract.py'
    for module in ['tbmb_md_production','tbmb_md_equilibrate','tbmb_md_minimize']:
        relative=f'tools/our_tools/{module}.py'
        if relative in source_hashes:
            required.add(relative)
    if (repo / contract).exists():
        required.add(contract)
    if not required <= source_hashes.keys():
        raise ValueError('incomplete_source_pin')
    for relative in required:
        if sha256(repo / relative) != source_hashes[relative]:
            raise ValueError('domain_source_changed')
    # Use the real project namespace; do not synthesize bioma modules or install
    # a second distribution. Domain source and the native package must agree.
    import bioma.tools.base as base
    if sha256(base.__file__) != source_hashes['src/bioma/tools/base.py']:
        raise ValueError('domain_package_source_mismatch')
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    if contract in required:
        import importlib
        module = importlib.import_module('tools.our_tools._crosslinked_impl.ensemble_contract')
        if sha256(module.__file__) != source_hashes[contract]:
            raise ValueError('domain_contract_source_mismatch')
    cross = load_module('peptide_domain_cross', repo / 'tools/our_tools/crosslinked_peptide_structure.py')
    docking = load_module('peptide_domain_docking', repo / 'tools/our_tools/tbmb_bicyclic_docking.py')
    tools = {}
    for name in TOOLS:
        fn = getattr(cross if hasattr(cross, name) else docking, name)
        tools[name] = fn._bioma_tool
    return tools, docking


def execute(folder, request):
    profile = request['profile']
    tools, docking = load_tools(profile['source_root'], profile['source_hashes'])
    original_popen = subprocess.Popen
    def allowed_popen(argv, *args, **kwargs):
        if kwargs.get('shell') or kwargs.get('start_new_session') or kwargs.get('preexec_fn') or isinstance(argv, str) or not argv:
            raise ValueError('shell_execution_rejected')
        binary = shutil.which(str(argv[0]))
        if not binary or profile.get('executables', {}).get(str(Path(binary).resolve())) != sha256(binary):
            raise ValueError('unregistered_executable')
        return original_popen(argv, *args, **kwargs)
    subprocess.Popen = allowed_popen
    staged = folder / 'working_inputs'
    staged.mkdir(exist_ok=True)
    mapped = {}
    for ref,item in request['inputs'].items():
        relative=Path(item.get('staged_path',ref + Path(item['path']).suffix))
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('invalid_staged_path')
        mapped[ref]=staged/relative
        mapped[ref].parent.mkdir(parents=True,exist_ok=True)
    if len(set(mapped.values()))!=len(mapped):raise ValueError('duplicate_staged_path')
    aliases = {}
    for ref, item in request['inputs'].items():
        aliases[item['source']] = str(mapped[ref])
        aliases[item['path']] = str(mapped[ref])
    def rewrite(value):
        if isinstance(value, dict):
            return {k: (str(folder / 'output') if k in {'workdir', 'output_dir'} else rewrite(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        if isinstance(value, str):
            if value in aliases:
                return aliases[value]
            if value.startswith('/'):
                if value in profile.get('executables', {}):
                    return value
                raise ValueError('unregistered_transitive_input')
            if '..' in Path(value).parts:
                raise ValueError('transitive_path_traversal')
        return value
    for ref, item in request['inputs'].items():
        source = folder / 'inputs' / ref
        if mapped[ref].suffix in {'.json', '.yaml', '.yml'}:
            import yaml
            obj = json.loads(source.read_text()) if mapped[ref].suffix == '.json' else yaml.safe_load(source.read_text())
            mapped[ref].write_text(json.dumps(rewrite(obj)))  # JSON is valid YAML.
        else:
            shutil.copyfile(source, mapped[ref])
    def fixed_run(command, log, cwd=None):
        # Legacy render produces a string; accept only a simple argv and pinned binary.
        argv = shlex.split(command)
        if not argv or any(token in {'|', '>', '>>', '<', '&&', ';', '&'} for token in argv):
            raise ValueError('shell_template_rejected')
        binary = Path(argv[0])
        approved = profile.get('executables', {})
        if not binary.is_absolute() or approved.get(str(binary)) != sha256(binary):
            raise ValueError('unregistered_executable')
        result = subprocess.run(argv, shell=False, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_bytes(result.stdout)
        if result.returncode:
            raise RuntimeError('domain_program_failed')
    docking._run = fixed_run
    # Agent cannot edit arguments. JSON/file references are mapped by trusted profile.
    def resolve(value):
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        if not isinstance(value, str):
            return value
        if value.startswith('$bundle:'):
            relative=Path(value.removeprefix('$bundle:'))
            if relative.is_absolute() or '..' in relative.parts:raise ValueError('invalid_bundle')
            path=staged/relative
            if not path.is_dir():raise ValueError('unregistered_bundle')
            return str(path)
        if value == '$output':
            return str(folder / 'output')
        if value.startswith('$input:'):
            ref = value.removeprefix('$input:')
            if ref not in request['inputs']:
                raise ValueError('unregistered_input')
            return str(mapped[ref])
        if value.startswith('$json:'):
            ref = value.removeprefix('$json:')
            if ref not in request['inputs']:
                raise ValueError('unregistered_input')
            obj = json.loads(mapped[ref].read_text())
            return json.dumps(resolve(obj))
        # A trusted profile must still register file paths instead of reaching outside.
        if value in {str(p) for p in mapped.values()} or value in profile.get('executables', {}):
            return value
        if value.startswith('/') or value.startswith('~') or '..' in Path(value).parts:
            raise ValueError('unregistered_path_in_profile')
        return value
    args = resolve(profile['arguments'])
    if profile['method'] in {'prepare_receptor', 'detect_pockets', 'diffdock_screen', 'vina_refine'}:
        import yaml
        query = yaml.safe_load(Path(args['query_path']).read_text())
        policy = profile['task_profile']
        if sorted(query['retain_chains']) != sorted(policy['retain_chains']):
            raise ValueError('receptor_profile_chain_mismatch')
    if profile['method']=='tbmb_md_production':
        for name in ['tbmb_md_minimize','tbmb_md_equilibrate','tbmb_md_production']:
            relative=f'tools/our_tools/{name}.py'
            if profile['source_hashes'].get(relative)!=sha256(Path(profile['source_root'])/relative):
                raise ValueError('unbound_md_source')
            module=load_module('tools.our_tools.'+name,Path(profile['source_root'])/relative)
        config=args['config']
        config['output_dir']=str(folder/'output'/'md')
        config.setdefault('runtime',{})['platform']='CUDA' if profile.get('gpu_ids') else 'CPU'
        if profile.get('gpu_ids'):
            config['runtime']['platform_properties']={'DeviceIndex':'0','Precision':'mixed'}
        scope=profile['production_scope']
        if (config['candidate_count']!=scope['candidate_count'] or config['replicas_per_candidate']!=scope['replicas_per_candidate']
                or config['production']['duration_ns']!=scope['ns_per_replica']):
            raise ValueError('approved_md_scope_mismatch')
        return module.run_production(config,staged,args.get('rank'),args.get('replica'),resume=True,overwrite=False)
    tool = tools[profile['method']]
    args = tool.args_schema.model_validate(args).model_dump()
    raw = tool.func(**args)
    return json.loads(raw) if isinstance(raw, str) else raw
