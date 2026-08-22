# NVIDIA-Ready Cluster Lifecycle

This repository provides three independent, opt-in layers for Kubernetes AI
clusters:

1. `kubernetes/addons/nvidia-ai-readiness` installs NVIDIA GPU Operator and
   blocks add-on readiness until one-GPU and full-node CUDA checks pass.
2. `kubernetes/bundles/workspace` creates retained model storage and proves a
   bounded write/read path.
3. `kubernetes/bundles/kubeflow-kserve` installs the pinned Kubeflow Pipelines,
   Model Registry, Katib, cert-manager, and KServe subset after cluster create.

The NVIDIA chart renders no objects by default. Cluster templates without an
`addon_profile` selector keep the normal mCAPI lifecycle. This is the required
compatibility path for Ubuntu 22.04, CPU-only images, and Kubernetes tags that
have not been accepted with the selected NVIDIA profile.

## Compatibility Contract

An operator must explicitly enable both `profile.enabled` and
`gpu-operator.enabled`. The readiness job then checks:

- Kubernetes server minor is at least `compatibility.minimumKubernetesMinor`;
- all observed nodes use Linux when `requireLinuxNodes=true`;
- all observed nodes use amd64 when `requireAmd64Nodes=true`;
- the expected number of GPU nodes and GPUs per node are allocatable; and
- real CUDA allocation succeeds first with one GPU and then the configured
  full-node count.

The preinstalled-driver path remains disabled unless
`allowPreinstalledDriverFallback=true` is explicitly reviewed together with a
compatible guest image. The managed-driver profile does not set this option.

## Offline Acceptance

Run these checks before publishing any chart or image:

```bash
./kubernetes/addons/nvidia-ai-readiness/test-render.sh
./kubernetes/bundles/workspace/test-render.sh
./kubernetes/bundles/kubeflow-kserve/test-contract.sh
```

The first check proves the default chart is inert, mutable wrapper images are
rejected, NRI remains disabled, deadlines and least-privilege RBAC are present,
and the observed readiness Pod image can be captured. These are render-time
checks only; they do not claim physical GPU acceptance.

## Runtime Acceptance

A successful add-on release must retain the evidence ConfigMap and prove:

- current-generation NVIDIA ClusterPolicy readiness;
- device-plugin DaemonSet rollout;
- exact expected GPU capacity and allocatable counts;
- one-GPU CUDA allocation and memory operation;
- full-node CUDA allocation and memory operations; and
- observed runtime image IDs for the readiness and GPU Operator Pods.

Install the workspace and Kubeflow/KServe bundles only after this lifecycle
gate passes. Their storage/data deletion paths remain separately controlled so
cluster add-on deletion cannot silently remove retained model artifacts.
