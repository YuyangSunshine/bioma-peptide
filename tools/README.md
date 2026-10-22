# Executable tools

`tools/` contains typed executable modules. `skills/` contains scientific guidance
and tool bindings; some skills also provide executable wrappers.

The default [configuration](../configs/bioma.yaml) selects `tools/our_tools`,
`tools/stella` and `tools/community`. The production [Factory](../src/bioma/providers/factory.py)
loads them through [ToolRegistry](../src/bioma/tools/registry.py), then loads skill
wrappers named `scripts/bioma_tools.py`. The loader discovers decorated callables carrying `_bioma_tool` metadata.
Underscore-prefixed modules are not discovery entry points; they can still be
imported helpers. ScientificComputeProvider receives the registry and grants
selected tools to MethodBackend execution.

- `our_tools/`: project scientific implementations and their helpers.
- `stella/`: retrieval, literature and screening integrations.
- `community/`: optional contributed tool modules.
- `manifests/`: provenance and inventory records.

Registration does not execute a scientific operation or authorize external access.
Dependencies, permissions, execution receipts and result qualification remain
separate requirements. See the [Method integration guide](../docs/method/method-integration.md).
