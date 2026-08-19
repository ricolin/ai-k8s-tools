# Kubernetes AI workflows

This tree provides the Kubernetes execution path for `ai-build-tools`.
Kubeflow Pipelines owns orchestration, Kubeflow Hub records model-version
metadata and lineage, and KServe owns inference deployment. The root-level
scripts provide the repository's bare-metal execution path; both paths use the
same immutable-input and release-evidence principles.

## Layout

```text
kubernetes/
├── profiles/                  # Environment-specific scheduling and evidence
├── platform/                  # Kubeflow, Hub, KServe and storage installation
├── workflows/                 # KFP DSL, workflow tools and unit tests
├── serving/                   # Custom base-plus-adapter mechanics runtime
├── manifests/                 # Namespace, RBAC and KServe resources
├── tools/                     # Provider-neutral build and run helpers
└── mcapi/                     # mCAPI egress and registry integration helpers
```

The default `kubernetes-fixture` profile has no provider-specific placement.
The `mcapi-emulated` example profile exercises the complete mechanics path
with deterministic fixture artifacts on a Kubernetes cluster that does not
advertise an NVIDIA CUDA resource:

```text
train candidate A
  -> generate and evaluate
  -> register A in Hub
  -> deploy A through KServe
  -> restart/fresh-load and verify
  -> mark A released
  -> train derived candidate B from A
  -> generate, evaluate and register B with parent=A
```

The `nvidia-h200` profile describes the scheduling and storage inputs expected
by a physical-GPU implementation. Fixture results are mechanics evidence and
must never be represented as physical GPU or model-quality evidence.

See [the full workflow and operations guide](../docs/kubernetes-workflow.md).
