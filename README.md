# AI Kubernetes Tools

Private development repository for reusable Kubernetes AI workflows. It keeps
Kubeflow, KServe, CUDA, H200, model-training, serving, and bounded security
research work separate from the standalone bare-metal workflow in
`ricolin/ai-build-tools`.

## Scope

- provider-neutral Kubeflow Pipelines and Model Registry lifecycle mechanics;
- mCAPI-compatible Kubernetes profiles and deployment helpers;
- physical NVIDIA H200 and CUDA acceptance templates;
- SDXL watercolor/detail A/B training and comparison jobs;
- Qwen security-adviser A/B/C training and vLLM/KServe serving contracts;
- an analysis-only security agent and public-source/runtime research sandbox;
- immutable evidence, dataset, source, release, and reports-only gates; and
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

Start with:

- [Kubernetes workflow](docs/kubernetes-workflow.md)
- [Kubernetes tools](kubernetes/README.md)
- [CUDA and H200 validation](kubernetes-CUDA/README.md)
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
  kubernetes-CUDA/security/*.sh \
  scripts/*.sh

python3 -m py_compile \
  kubernetes-CUDA/image/*.py \
  kubernetes-CUDA/security/*.py

./kubernetes/tools/verify-security-research-implementation.sh \
  /tmp/ai-k8s-tools-local-evidence
```

The final verifier proves local contracts and a synthetic fixture only. It
does not prove CUDA, H200 performance, model training quality, KServe on a
live cluster, or production readiness.

## Repository split

The original `kubernetes/` and `kubernetes-CUDA/` histories from
`ricolin/ai-build-tools` remain reachable in this repository. New Kubernetes
work belongs here. Shared standalone GPU bootstrap behavior may be synchronized
deliberately, but Kubernetes code must not regain a dependency on the
standalone repository.
