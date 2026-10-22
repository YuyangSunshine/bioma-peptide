from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any


from bioma.core.protocols import VerificationReport
from bioma.core.measurements import simulation_payload
from bioma.backends.automation.adapters import AutomationCompiler


class MachineCodeValidator:
    VALID_RESOURCES = {-1, *range(48, 70)}

    def validate(self, code: dict[str, Any]) -> VerificationReport:
        issues: list[str] = []
        for field_name in ("nodes", "consumables", "connections"):
            if not isinstance(code.get(field_name), list):
                issues.append(f"{field_name} must be a list")
        nodes = code.get("nodes") if isinstance(code.get("nodes"), list) else []
        if not nodes:
            issues.append("machine code needs at least one node")
        node_ids: set[int] = set()
        for node in nodes:
            if not isinstance(node, dict):
                issues.append("every node must be an object")
                continue
            node_id = node.get("templateNodeId")
            resource_id = node.get("resourceId")
            if not isinstance(node_id, int):
                issues.append("every node needs an integer templateNodeId")
            elif node_id in node_ids:
                issues.append(f"duplicate templateNodeId: {node_id}")
            else:
                node_ids.add(node_id)
            if resource_id not in self.VALID_RESOURCES:
                issues.append(f"invalid resourceId: {resource_id}")
            if not isinstance(node.get("parameters"), dict):
                issues.append(f"node {node_id} parameters must be an object")
        connections = (
            code.get("connections") if isinstance(code.get("connections"), list) else []
        )
        edges: list[tuple[int, int]] = []
        for connection in connections:
            if not isinstance(connection, dict):
                issues.append("every connection must be an object")
                continue
            source = connection.get("sourceNodeId")
            target = connection.get("targetNodeId")
            if source not in node_ids or target not in node_ids:
                issues.append(f"invalid connection: {source}->{target}")
            elif source == target:
                issues.append(f"self-cycle at node {source}")
            else:
                edges.append((source, target))
        if _has_cycle(node_ids, edges):
            issues.append("machine code connections contain a cycle")
        return VerificationReport(not issues, tuple(issues))

    def rectify(self, code: dict[str, Any], issues: tuple[str, ...]) -> dict[str, Any]:
        fixed = json.loads(json.dumps(code))
        for field_name in ("nodes", "consumables", "connections"):
            if not isinstance(fixed.get(field_name), list):
                fixed[field_name] = []
        fixed["nodes"] = [node for node in fixed["nodes"] if isinstance(node, dict)]
        fixed["connections"] = [
            connection
            for connection in fixed["connections"]
            if isinstance(connection, dict)
        ]
        valid_ids = {
            node.get("templateNodeId")
            for node in fixed["nodes"]
            if isinstance(node.get("templateNodeId"), int)
        }
        fixed["connections"] = [
            item
            for item in fixed["connections"]
            if item.get("sourceNodeId") in valid_ids
            and item.get("targetNodeId") in valid_ids
            and item.get("sourceNodeId") != item.get("targetNodeId")
        ]
        fixed["rectified_issues"] = list(issues)
        return fixed


class ToyAutomationCompiler(AutomationCompiler):
    def __init__(self, validator: MachineCodeValidator | None = None) -> None:
        self.validator = validator or MachineCodeValidator()

    def align(self, protocol_text: str, objective: str) -> str:
        return (
            "1. [AUTO] Liquid handling: prepare candidate and wild_type samples.\n"
            "2. [AUTO] Plate reading: measure fluorescence after thermal challenge."
        )

    def generate(
        self,
        aligned_protocol: str,
        objective: str,
        protocol_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        return {
            "protocolId": protocol_id,
            "nodes": [
                {
                    "templateNodeId": 1,
                    "resourceId": 48,
                    "parameters": {"action": "prepare samples", "replicates": 3},
                },
                {
                    "templateNodeId": 2,
                    "resourceId": 55,
                    "parameters": {
                        "action": "measure fluorescence after thermal challenge",
                        "temperature_c": 60,
                        "duration_min": 30,
                    },
                },
            ],
            "consumables": [],
            "connections": [{"sourceNodeId": 1, "targetNodeId": 2}],
        }

    def validate(self, machine_code: dict[str, Any]) -> VerificationReport:
        return self.validator.validate(machine_code)

    def rectify(
        self,
        machine_code: dict[str, Any],
        issues: tuple[str, ...],
        task_id: str,
    ) -> dict[str, Any]:
        return self.validator.rectify(machine_code, issues)


class ProtocolSimulator:
    """Virtual device journal and explicit data replay, not a biological model.

    A transaction records all virtual node effects and the receipt together.
    Reopening the same journal preserves action identity across process restarts.
    """
    def __init__(self, journal_path=None):
        if journal_path is not None:
            Path(journal_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(journal_path) if journal_path else ':memory:',
                                   check_same_thread=False, timeout=30)
        self._lock = RLock()
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS simulation_jobs (
                action_id TEXT PRIMARY KEY, contract_hash TEXT NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS simulation_resources (
                resource_id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        ''')

    def close(self):
        self._db.close()

    def set_resource_status(self, resource_id, status):
        if status not in {'ready', 'busy', 'offline'}:
            raise ValueError('Unknown virtual resource status')
        with self._lock, self._db:
            self._db.execute('INSERT OR REPLACE INTO simulation_resources VALUES (?, ?)',
                             (resource_id, status))

    def completed_jobs(self):
        with self._lock:
            return self._db.execute('SELECT COUNT(*) FROM simulation_jobs').fetchone()[0]

    def reconcile(self, code, inputs):
        payload, context, contract = self._contract(code, inputs)
        with self._lock:
            row = self._db.execute('SELECT contract_hash, result FROM simulation_jobs WHERE action_id=?',
                                   (context['action_id'],)).fetchone()
        if row is None:
            return None
        if row[0] != contract:
            raise ValueError('Simulation action identity reused with different inputs or machine code')
        return json.loads(row[1])

    def _contract(self, code, inputs):
        payload = simulation_payload(inputs)
        context = inputs.get('_simulation_context')
        if not isinstance(context, dict) or not all(context.get(k) for k in ('task_id','action_id','protocol_sha256')):
            raise ValueError('Simulation requires task, action and reviewed protocol identity')
        return payload, context, _digest({'code':code, 'payload':payload, 'context':context})

    def execute(
        self,
        code: dict[str, Any],
        simulation_inputs: dict[str, Any],
    ) -> dict[str, Any]:
        report = MachineCodeValidator().validate(code)
        if not report.passed:
            raise ValueError('Invalid simulation graph: ' + '; '.join(report.issues))
        payload, context, contract = self._contract(code, simulation_inputs)
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                previous = self.reconcile(code, simulation_inputs)
                if previous is not None:
                    self._db.commit()
                    return previous
                nodes = {node['templateNodeId']:node for node in code['nodes']}
                pending, done, trace = set(nodes), set(), []
                while pending:
                    ready = sorted(n for n in pending if all(e['sourceNodeId'] in done
                        for e in code['connections'] if e['targetNodeId'] == n))
                    if not ready:
                        raise ValueError('Simulation graph cannot progress')
                    for node_id in ready:
                        node = nodes[node_id]
                        status = self._db.execute('SELECT status FROM simulation_resources WHERE resource_id=?',
                                                  (node['resourceId'],)).fetchone()
                        if status and status[0] != 'ready':
                            raise RuntimeError('Virtual instrument ' + str(node['resourceId']) + ' is ' + status[0])
                        if payload.get('fault') == 'node_failure':
                            raise RuntimeError('Injected virtual node failure; transaction rolled back')
                        trace.append({'node_id':node_id, 'resource_id':node['resourceId'],
                            'parameters_sha256':_digest(node['parameters']), 'status':'completed',
                            'virtual_tick':len(trace) + 1})
                        pending.remove(node_id); done.add(node_id)
                result = {'mode':'simulation', 'data_kind':'simulation', 'executed':True,
                    'physical_execution':False, 'simulation_model':'explicit-data-replay-v1',
                    'node_count':len(nodes), 'measurements':payload['measurements'],
                    'source':payload['source'], 'node_trace':trace,
                    'receipt':{**context, 'protocol_id':code.get('protocolId'), 'contract_sha256':contract,
                        'machine_code_sha256':_digest(code), 'measurements_sha256':_digest(payload['measurements']),
                        'trace_sha256':_digest(trace), 'status':'completed', 'physical_execution':False}}
                self._db.execute('INSERT INTO simulation_jobs VALUES (?, ?, ?)',
                    (context['action_id'], contract, json.dumps(result, ensure_ascii=False, allow_nan=False)))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        if payload.get('fault') == 'lost_response':
            raise TimeoutError('Injected response loss after virtual device committed')
        return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _has_cycle(nodes: set[int], edges: list[tuple[int, int]]) -> bool:
    outgoing: dict[int, list[int]] = {node: [] for node in nodes}
    indegree = dict.fromkeys(nodes, 0)
    for source, target in edges:
        outgoing[source].append(target)
        indegree[target] += 1
    ready = [node for node, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for target in outgoing[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    return visited != len(nodes)
