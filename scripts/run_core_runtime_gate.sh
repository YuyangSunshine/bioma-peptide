#!/usr/bin/env bash
set -euo pipefail
python -m pytest -q \
  tests/test_credential_templates.py \
  tests/test_evidence_driven_control_integration.py \
  tests/test_scientific_control_runtime.py \
  tests/test_scientific_controller.py \
  tests/test_scientific_option_generation_revision.py \
  tests/test_llm_temperature.py \
  tests/test_shared_capability_negative_gates.py \
  tests/test_scoped_controller_integration.py \
  tests/test_scoped_skill_resolver.py \
  tests/test_execution_budget.py \
  tests/test_replay_safe_effects.py \
  tests/test_work_result_boundary.py \
  tests/test_method_v2_authority_boundaries.py \
  tests/protocol/test_admission_gates.py \
  tests/protocol/test_receipt_and_effects.py \
  tests/protocol/test_wp2_semantic_guards.py
