# AI Kubernetes Tools

Public, reusable Kubernetes AI workflow tooling. It keeps
Kubeflow, KServe, CUDA, H200, model-training, serving, and bounded security
research work separate from the standalone bare-metal workflow in
`ricolin/ai-build-tools`.

## Scope

- provider-neutral Kubeflow Pipelines and Model Registry lifecycle mechanics;
- mCAPI-compatible Kubernetes profiles and deployment helpers;
- physical NVIDIA H200 and CUDA acceptance templates;
- KFP-owned SDXL watercolor/Impressionist A/B training through Kubeflow
  Trainer, JobSet, and Kueue, followed by queued comparison galleries;
- KFP-owned Qwen code-review A/B/C training through the same admission path
  for Bash, Python, Go, Rust, and YAML;
- a sandboxed repository and pull-request agent that can test and export a
  candidate `fix.patch` without pushing or publishing it;
- retained Qwen security-adviser A/B/C and research resources in separate
  `security` paths;
- an analysis-only security agent and public-source/runtime research sandbox;
- immutable evidence, dataset, source, release, and reports-only gates; and
- explicit `TRAINING_COMPLETE`, `WORKFLOW_VALIDATED`, `QUALITY_REJECTED`,
  `SERVING_CANARY`, and `PRODUCTION_APPROVED` lifecycle enforcement; and
- local fixture validation that does not claim physical GPU proof.

This repository does not contain a site-specific address, kubeconfig,
credential, private training dataset, model weight, registry token, or
customer finding. Environment-specific commands and evidence stay in the
corresponding private operations records.

## Layout

```text
docs/                  Generic workflow documentation
kubernetes/            Provider-neutral orchestration and policy contracts
kubernetes-CUDA/       Physical CUDA training and validation backends
scripts/                Shared GPU runtime bootstrap helper
```

Cluster lifecycle add-ons are opt-in and render no resources by default. The
NVIDIA readiness profile checks Kubernetes/Linux/architecture compatibility at
runtime, and clusters that do not select the profile retain their existing
guest-image and Kubernetes-tag behavior.

Start with:

- [Kubernetes workflow](docs/kubernetes-workflow.md)
- [Code-review model workflow](docs/code-review-workflow.md)
- [Code-review patch-and-test agent](docs/code-review-agent-workflow.md)
- [Grounded security-agent workflow](docs/security-agent-workflow.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Kubernetes tools](kubernetes/README.md)
- [CUDA and H200 validation](kubernetes-CUDA/README.md)
- [NVIDIA-ready cluster lifecycle](docs/nvidia-ai-ready-cluster.md)
- [SDXL CUDA backend](kubernetes-CUDA/image/README.md)
- [Security adviser CUDA backend](kubernetes-CUDA/security/README.md)

## Local validation

```bash
uv sync --project kubernetes/workflows --python 3.12 --frozen
uv run --project kubernetes/workflows --frozen \
  pytest -q kubernetes/workflows/tests

bash -n \
  kubernetes/tools/*.sh \
  kubernetes/mcapi/*.sh \
  kubernetes/platform/*.sh \
  kubernetes-CUDA/image/*.sh \
  kubernetes-CUDA/code-review/*.sh \
  kubernetes-CUDA/security/*.sh \
  scripts/*.sh

python3 -m py_compile \
  kubernetes-CUDA/image/*.py \
  kubernetes-CUDA/code-review/*.py \
  kubernetes-CUDA/security/*.py

./kubernetes/tools/verify-security-research-implementation.sh \
  /tmp/ai-k8s-tools-local-evidence

./kubernetes/addons/nvidia-ai-readiness/test-render.sh
./kubernetes/bundles/workspace/test-render.sh
./kubernetes/bundles/kubeflow-kserve/test-contract.sh
./kubernetes/platform-profiles/test.sh
```

The final verifier proves local contracts and a synthetic fixture only. It
does not prove CUDA, H200 performance, model training quality, KServe on a
live cluster, or production readiness.

For each command's acceptance condition and failure boundary, follow the
[security-agent workflow](docs/security-agent-workflow.md). Scanner execution
and KServe lifecycle are operator integrations; this repository validates the
evidence and release contracts and does not silently grant authority to scan a
target.

## Repository split

The original `kubernetes/` and `kubernetes-CUDA/` histories from
`ricolin/ai-build-tools` remain reachable in this repository. New Kubernetes
work belongs here. Shared standalone GPU bootstrap behavior may be synchronized
deliberately, but Kubernetes code must not regain a dependency on the
standalone repository.
