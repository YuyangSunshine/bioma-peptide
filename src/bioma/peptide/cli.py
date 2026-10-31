import argparse
import json
from .broker import Broker
from .catalog import catalog


def main():
    p = argparse.ArgumentParser(description='Peptide contracts and persistent local scientific jobs')
    sub = p.add_subparsers(dest='command', required=True)
    c = sub.add_parser('ingest'); c.add_argument('--input', required=True); c.add_argument('--output', required=True)
    c = sub.add_parser('catalog'); c.add_argument('--source-root', required=True)
    for command in ['submit', 'status', 'collect', 'cancel', 'resume', 'reattach']:
        c = sub.add_parser(command)
        c.add_argument('--root', required=True); c.add_argument('--registry', required=True)
        if command == 'submit':
            c.add_argument('--request', required=True, help='Structured JSON request; no shell command')
        else:
            c.add_argument('--job-id', required=True)
    a = p.parse_args()
    if a.command == 'ingest':
        from pathlib import Path
        from .data import measurement
        rows = [measurement(json.loads(line)) for line in Path(a.input).read_text().splitlines() if line.strip()]
        with Path(a.output).open('x') as output:
            for row in rows:
                output.write(json.dumps(row, allow_nan=False) + '\n')
        result = {'records': len(rows), 'output': str(Path(a.output).resolve()), 'original_retained': True}
    elif a.command == 'catalog':
        result = catalog(a.source_root)
    else:
        broker = Broker(a.root, a.registry)
        try:
            if a.command == 'submit':
                from pathlib import Path
                result = broker.submit(**json.loads(Path(a.request).read_text()))
            else:
                result = getattr(broker, a.command)(a.job_id)
        except (ValueError, KeyError, OSError) as exc:
            reason = str(exc)
            print(json.dumps({'schema_version': '1.0', 'stage_status': reason.split(':',1)[0],
                              'execution_status': 'blocked' if reason.startswith('blocked_') else 'failed',
                              'failure_reasons': [reason]}))
            raise SystemExit(2) from None
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
