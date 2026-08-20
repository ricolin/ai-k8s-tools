# CUDA and H200 Kubernetes validation

This directory is the entry point for proving the `ai-k8s-tools/kubernetes`
workflow on a physical NVIDIA H200 node. It is separate from `kubernetes/`
until the CUDA training, derived-training, generation, and serving gates have
all passed on real hardware.

The existing Kubernetes fixture proves workflow mechanics only. Applying an
H200 node label to an emulated node does not prove CUDA, GPU allocation,
multi-GPU training, model quality, or GPU-backed inference.

## Contents

```text
kubernetes-CUDA/
├── README.md
├── validation-plan.md
└── templates/
    ├── h200-validation.env.example
    ├── nvidia-smi-pod.yaml
    ├── pytorch-all-gpu-job.yaml
    └── pytorch-cuda-job.yaml
```

- [validation-plan.md](validation-plan.md) is the reproducible implementation
  and validation runbook.
- [`../scripts/bootstrap_gpu_runtime.sh`](../scripts/bootstrap_gpu_runtime.sh)
  installs the accepted Ubuntu server-open driver, Fabric Manager, matching
  kernel headers/extras, and NVIDIA Container Toolkit. It verifies that the
  NVIDIA module exists for the selected boot kernel before reporting success.
  Set
  `CONTAINER_RUNTIME=containerd` for Kubernetes nodes; its default remains
  `docker` for the standalone image workflow.
- `h200-validation.env.example` separates training, generation, and serving
  GPU counts and supplies site-owned scheduling values.
- `nvidia-smi-pod.yaml` proves that Kubernetes can allocate one GPU and expose
  it to a container.
- `pytorch-cuda-job.yaml` proves PyTorch CUDA execution and records the device
  identity seen by the container.
- `pytorch-all-gpu-job.yaml` requests the configured training GPU count and
  performs an independent CUDA tensor operation on every allocated device. It
  proves whole-node visibility but does not claim DDP or NCCL qualification.

The validation workloads tolerate the standard control-plane taint because a
single physical H200 may provide both the control plane and the validation
capacity. The H200 node selector remains mandatory, so this does not permit
the workloads to spill onto ordinary control-plane nodes.

When kubelet uses the Static Memory Manager, install the runtime only during a
drained maintenance window. Loading the GPU driver can change the NUMA memory
map and invalidate `/var/lib/kubelet/memory_manager_state`; follow the guarded
checkpoint procedure in [validation-plan.md](validation-plan.md) before the
required reboot.

## Intended workflow

```text
physical H200 acceptance
  -> one-GPU CUDA smoke
  -> one-GPU SDXL LoRA pilot
  -> eight-GPU single-node training
  -> immutable release A
  -> new training run loading release A
  -> immutable derived release B
  -> one-GPU generation and evaluation
  -> one-GPU KServe fresh-process load and inference
```

Release B is derived from the immutable LoRA artifact in release A. This is
different from resuming an interrupted optimizer checkpoint inside one
training run. Optimizer checkpoint/resume can be added later without changing
the A-to-B release lineage contract.

## Proof states

Use the highest state whose gate has actually passed:

| State | Meaning |
| --- | --- |
| `mechanics` | KFP, artifact, registry, and KServe fixture flow passed without CUDA. |
| `cuda-smoke` | A Kubernetes pod requested `nvidia.com/gpu` and completed a CUDA operation. |
| `single-gpu` | Real SDXL pilot, generation, and evaluation passed on one H200 GPU. |
| `single-node-ddp` | One training run used the requested eight H200 GPUs on one node. |
| `physical-release` | Releases A and B, fresh KServe loading, inference, lineage, and immutable evidence all passed. |

Do not set `physical-release` in a profile. The validation runner must derive
it from observed Kubernetes resources and successful gate evidence.

## Start here

Copy the profile and replace every placeholder:

```bash
cp kubernetes-CUDA/templates/h200-validation.env.example \
  /path/to/site-owned-h200.env

set -a
. /path/to/site-owned-h200.env
set +a
```

Then follow [the validation runbook](validation-plan.md). Every image used in
an accepted run must be referenced by digest, every retry must get a new run
ID, and prior evidence must remain unchanged.
