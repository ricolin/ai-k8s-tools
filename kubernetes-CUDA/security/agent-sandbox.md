# Sandboxed Agent Contract Validation

Validate the agent policy broker only against synthetic frozen evidence. Use a
dedicated namespace with Pod Security `restricted`, default-deny ingress and
egress, no service-account token, no host access, no privilege, no GPU, and no
external endpoint.

Run one positive fixture that emits reports/evidence only, then one negative
fixture containing a prohibited `command` argument. Required outcomes are:

```text
positive: status PASS, analysis_only true, reports_and_evidence_only true
negative: contract error: prohibited task arguments: command
```

The fixture release identity is synthetic and must never be treated as an
accepted model. A real adviser can be connected only after its deterministic
quality gate and release identity checks pass. This validation does not scan a
container, repository, site, cluster, or public endpoint and does not execute
arbitrary shell commands.
