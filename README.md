# Kubernetes AI workflows

This tree adapts the accepted `ai-build-tools` SDXL LoRA method to Kubernetes
without introducing the former Track 2 or Track 3 services. Kubeflow Pipelines
owns orchestration and lineage, Kubeflow Hub owns model-version metadata, and
KServe owns the inference deployment.

The root-level scripts remain the accepted direct-host S4 workflow. They are
not moved or silently changed by this integration.

## Layout

```text
kubernetes/
├── profiles/                  # Environment-specific scheduling and evidence
├── platform/                  # Kubeflow, Hub, KServe and storage installation
├── workflows/                 # KFP DSL, workflow tools and unit tests
├── serving/                   # Custom base-plus-adapter mechanics runtime
├── manifests/                 # Namespace, RBAC and KServe resources
└── aio/                       # Retained mCAPI AIO build/run helpers
```

The AIO profile exercises the complete mechanics path with deterministic
fixture artifacts because its virtio GPU is not an NVIDIA CUDA device:

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

The physical profile replaces only the fixture training and serving images
with the accepted CUDA/Diffusers images and requests real `nvidia.com/gpu`
resources. An AIO PASS is mechanics evidence, never physical GPU evidence.

See [the full workflow and operations guide](../docs/kubernetes-workflow.md).
