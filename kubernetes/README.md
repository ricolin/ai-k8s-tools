# Kubernetes AI workflows

This tree provides the provider-neutral execution path for `ai-k8s-tools`.
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
Physical CUDA integration remains a separate gated workstream; its detailed
plan and reusable H200 validation templates are in
[kubernetes-CUDA](../kubernetes-CUDA/README.md).

The released-adviser and reports-only agent path is documented in
[the grounded security-agent guide](../docs/security-agent-workflow.md), with
failure recovery in [troubleshooting](../docs/troubleshooting.md).

The code-review track is separate from that retained security workflow:

- `kubernetes/code-review` contains portable review, release, agent, and
  sandbox contracts;
- `kubernetes-CUDA/code-review` contains the H200 dataset, trainer image,
  deterministic comparison, and quality gate; and
- [code-review workflow](../docs/code-review-workflow.md) and
  [code-review agent workflow](../docs/code-review-agent-workflow.md) document
  A/B/C training, repository or pull-request review, sandbox patching, unit
  tests, and `fix.patch` export.

## Rendered manifest validation

Shell-rendered manifests must be checked before they reach the API server. The
validator rejects missing files and empty container image values, including
quoted empty strings:

```bash
./kubernetes/tools/validate-rendered-manifest.sh /path/to/rendered.yaml
kubectl apply --dry-run=server -f /path/to/rendered.yaml
```

Use both gates. The repository validator gives a focused error for accidental
empty image variables; the API server dry run validates the complete resource
against the target cluster.

## Pinned operator runtime

`kubernetes/tools/ai-workflow` requires the repository-pinned `uv` runtime. On
an operator host without `uv`, install it atomically and retain the downloaded
archive checksum with the run evidence:

```bash
sudo ./kubernetes/tools/install-uv.sh \
  /path/to/run-evidence/source/tools \
  /opt/ai-build-tools-bin/uv
export UV_BIN=/opt/ai-build-tools-bin/uv
"${UV_BIN}" --version
```

Persist `UV_BIN` in the run ledger. The workflow wrapper also discovers the
standard `/opt/ai-build-tools-bin/uv` path, but an explicit exported value
makes resumed shells reproducible.
