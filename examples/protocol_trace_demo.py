"""End-to-end demo of the Scientific Transition Protocol trace.

Runs one deterministic (toy) scientific transition through the real runtime and
prints the identifier chain the protocol guarantees:

    request -> offer -> commitment -> proposal -> mandate -> receipt
    -> evidence -> verification -> commit

No LLM, no device, no network. The ledger is written to a temporary directory
so the repository is left untouched.

Run with:  python examples/protocol_trace_demo.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from bioma.runtime.participant import RuntimeParticipant
from bioma.core.models import (
    CapabilitySpec,
    Evidence,
    ScientificState,
    ScientificTask,
    StateDelta,
)
from bioma.core.protocol_events import MISSING_EVENTS, stage_of
from bioma.core.protocol_trace import REQUIRED_STAGES, build_chains
from bioma.runtime.engine import DecentralizedRuntime

CAPABILITY_ID = "scientific.protocol_compute"
METHOD_ID = "compute_metric"
MESSAGE_TYPE = "task.submitted"


class ProtocolActor(RuntimeParticipant):
    """A deterministic actor that proposes one bounded, observable write."""

    def __init__(self, name: str = "predagent") -> None:
        super().__init__([name])
        self.owner = name
        self.advertise(_capability(name))

    def handle(self, state, message):
        return StateDelta(
            agent_id=self.owner,
            source_message_id=message.id,
            expected_version=state.version,
            updates={"metrics": {"value": 1}},
            evidence=[
                Evidence(
                    kind="prediction",
                    source=self.owner,
                    claim="computed one bounded metric",
                )
            ],
            outcome="completed",
        )


def _capability(owner: str) -> CapabilitySpec:
    return CapabilitySpec(
        id=CAPABILITY_ID,
        agent_id=owner,
        description="Deterministic protocol capability for the trace demo",
        accepts_message_types=[MESSAGE_TYPE],
        max_risk_level="low",
        methods=[
            dict(
                id=METHOD_ID,
                description="Compute one bounded metric",
                message_type=MESSAGE_TYPE,
                result=dict(
                    responsibility=CAPABILITY_ID,
                    evidence_kinds=["prediction"],
                    verification_obligations=["transition_integrity"],
                ),
                action_contract=dict(
                    writable_fields=["metrics"],
                    risk_level="low",
                    expected_effects=[
                        dict(
                            id="produced",
                            source="result",
                            path="evidence",
                            operator="nonempty",
                        )
                    ],
                ),
            )
        ],
    )


def _task() -> ScientificTask:
    return ScientificTask(
        objective="compute a bounded metric",
        initial_capability=CAPABILITY_ID,
        initial_message_type=MESSAGE_TYPE,
        max_cycles=3,
    )


def _print_chain(chain) -> None:
    print(f"   stages            : {' -> '.join(chain.stages)}")
    print(f"   missing stages    : {list(chain.missing_stages) or 'none'}")
    print(f"   requests          : {list(chain.request_ids)}")
    print(f"   offers            : {list(chain.offer_ids)}")
    print(f"   commitments       : {list(chain.commitment_ids)}")
    print(f"   proposals         : {list(chain.proposal_ids)}")
    print(f"   mandates          : {list(chain.mandate_ids)}")
    print(f"   receipts          : {list(chain.receipt_ids)}")
    print(f"   evidence          : {list(chain.evidence_ids)}")
    print(f"   verifications     : {list(chain.verification_ids)}")
    print(f"   commit versions   : {[list(v) for v in chain.commit_versions]}")
    print(f"   complete          : {chain.complete()}")


def main() -> None:
    print("BioMA Scientific Transition Protocol trace")
    print("=" * 64)

    with tempfile.TemporaryDirectory(prefix="bioma-protocol-") as tmp:
        runtime = DecentralizedRuntime(
            [ProtocolActor("predagent")], ledger_path=Path(tmp) / "run.db"
        )
        task = _task()
        events: list[dict] = []
        state: ScientificState = runtime.run(task, event_sink=events.append)

        print("1. Committed result")
        print(f"   status  : {state.status}")
        print(f"   version : {state.version}")
        print(f"   metrics : {state.metrics}")

        print("\n2. Protocol trace (from durable events)")
        chain = build_chains(runtime.ledger.events(task.id), sink_events=events)
        _print_chain(chain)

        assert state.status == "completed", state.status_reason
        assert chain.complete(), chain.missing_stages
        assert chain.stages == REQUIRED_STAGES

        print("\n3. Identity is derived, never stored")
        mandate = chain.mandate_ids[0]
        receipt = chain.receipt_ids[0]
        proposal = chain.proposal_ids[0]
        print(f"   proposal : {proposal}")
        print(f"   mandate  : {mandate}  (== 'mandate:' + proposal)")
        print(f"   receipt  : {receipt}  (== 'receipt:' + proposal + ':' + delta_hash)")
        assert mandate == "mandate:" + proposal
        assert receipt.startswith("receipt:" + proposal + ":")

        print("\n4. Two-layer boundary")
        print("   Layer A (runtime event stream): request, offer, commitment")
        print("   Layer B (ledger journal)      : proposal, admission, execution,")
        print("                                   verification, commit")
        for kind in sorted({event["type"] for event in events}):
            print(f"   {str(stage_of(kind)):>7}  {kind}")

        print("\n5. Paper events with no distinct runtime event today")
        if MISSING_EVENTS:
            for name in MISSING_EVENTS:
                print(f"   reported, not invented: {name}")
        else:
            print("   none: every paper event has a distinct runtime event")

        print("\nVERDICT: PASS - the protocol chain is complete and reproducible.")


if __name__ == "__main__":
    main()
