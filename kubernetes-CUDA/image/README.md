# SDXL A/B CUDA backend

This backend implements the physical SDXL path described by the Kubernetes
workflow plan. Release A trains a watercolor LoRA. Release B can train either
a compatible detail LoRA or an Impressionist progression LoRA while a
verified, immutable A is active and frozen.

For B, the launcher creates an ephemeral foundation by loading A at the
configured scale, fusing it into an in-memory copy of SDXL, and saving that
copy under the run output. The official, commit-pinned Diffusers trainer then
optimizes only the new B LoRA against that frozen composed foundation. The
accepted Impressionist B serving identity is:

```text
original SDXL foundation + immutable A-watercolor + immutable B-impressionism
```

The ephemeral fused foundation is training implementation state, not a new
release foundation. Parent A is hashed before and after composition/training.

## Build

Both the PyTorch CUDA base image and Diffusers source commit are mandatory
immutable inputs:

```bash
revision=$(git rev-parse HEAD)
base=registry.example/pytorch@sha256:REPLACE
diffusers_commit=REPLACE_WITH_REVIEWED_40_CHARACTER_COMMIT
out=evidence/build-image-workflow-${revision:0:12}

./kubernetes-CUDA/image/build-image.sh \
  "${revision}" "${base}" "${diffusers_commit}" "${out}" \
  registry.example/ai/image-workflow

# Review build-command.sh, then publish.
./kubernetes-CUDA/image/build-image.sh \
  "${revision}" "${base}" "${diffusers_commit}" "${out}" \
  registry.example/ai/image-workflow --push
. "${out}/image.env"
```

## Dataset preparation

The source manifest records image identity, caption, source, license,
permission, split, and stage. Prepare a read-only Hugging Face `imagefolder`
dataset without mutating the source:

```bash
python kubernetes-CUDA/image/prepare_dataset.py \
  --dataset-root /path/to/source-dataset \
  --manifest /path/to/source-dataset/manifest.jsonl \
  --stages A \
  --output /workspace/datasets/release-a-imagefolder
```

For Impressionist B, use `--stages B-impressionism,A-replay`. The legacy detail
path remains available as `B-detail,A-replay`. Resolve replay weighting in the
manifest's bounded `sampling_weight` before preparation. Every prepared copy
retains its source and license evidence.

For a deterministic demonstration corpus:

```bash
python kubernetes-CUDA/image/generate_demo_dataset.py \
  --output /workspace/datasets/watercolor-impressionism \
  --a-count 96 --b-count 96 --replay-count 24 \
  --b-style impressionism --seed 260821
```

This procedural corpus is useful for workflow validation only. A production
release requires a curated, licensed dataset and independent quality review.

## Foundation materialization

Pin the Hugging Face repository revision, then hash the model payload rather
than client transport metadata. `huggingface-cli download --local-dir` may
create a `.cache` directory whose contents depend on the client and transfer
history. Preserve its file list as acquisition evidence, remove only that
transport directory, and seal the remaining model tree before training.

The resulting payload digest is the `base_digest` consumed by every training
and generation configuration. Do not compare a raw download tree that still
contains `.cache` with a normalized payload tree.

## Run through KFP, Trainer, and Kueue

The supported workflow compiles one KFP DAG. Release A and B are submitted as
Kubeflow Trainer `TrainJob` resources and admitted by Kueue. Foundation, A,
and B galleries are queued batch Jobs after training completes. The default
seven-GPU training request preserves one of eight H200 GPUs for KServe.

```bash
./kubernetes/tools/ai-workflow image-pipeline \
  --workflow-image registry.example/ai-workflow@sha256:<digest> \
  --output evidence/image-pipeline.yaml
```

Supply the exact run arguments and KFP endpoint to the same command to submit
the package. The workflow container components do not request GPUs; they own,
wait for, and retain evidence from the GPU workloads.

## Break-glass direct Jobs

The direct renderer remains available for diagnosis when KFP or Trainer is
unhealthy. It is not the accepted end-to-end training path.

```bash
./kubernetes/tools/ai-workflow image render-job \
  --name image-release-a-train \
  --namespace ai-workflows \
  --image "${CUDA_IMAGE_WORKFLOW_IMAGE}" \
  --pvc ai-model-workspace \
  --config-path /workspace/configs/image-A.json \
  --gpu-count 7 \
  --mode train \
  --node-selector-key nvidia.com/gpu.product \
  --node-selector-value NVIDIA-H200 \
  --output evidence/image-release-a-job.json

./kubernetes/tools/ai-workflow image render-job \
  --name image-release-a-gallery \
  --namespace ai-workflows \
  --image "${CUDA_IMAGE_WORKFLOW_IMAGE}" \
  --pvc ai-model-workspace \
  --config-path /workspace/configs/generate-A.json \
  --gpu-count 1 \
  --mode generate \
  --node-selector-key nvidia.com/gpu.product \
  --node-selector-value NVIDIA-H200 \
  --output evidence/image-release-a-gallery-job.json
```

Foundation, A, and B generation configs must reference the same immutable
comparison prompt manifest. The renderer and generator do not add
release-specific prompt text. Release C remains a separate optional
cross-backbone implementation and must not reuse SDXL adapter tensors.

Training writes `training-command.json` and `training-result.json` beside the
adapter directory. Automation must validate `training-result.json`; it records
the base, dataset, parent, config, adapter, GPU-count, and effective-batch
identities needed to resume a completed stage without retraining it.

## Serve A Digest-Bound Release

The image runtime also contains `serve_model.py`. It loads the normalized SDXL
foundation plus one or two named LoRA adapters, verifies the foundation tree and
adapter file digests before becoming Ready, and exposes a KServe-compatible
prediction endpoint. Render the one-GPU, node-local InferenceService from the
same locked source:

```bash
./kubernetes/tools/ai-workflow image render-node-local-serving \
  --name image-b-watercolor-impressionism \
  --namespace kubeflow \
  --image ai-k8s-tools.local/image-workflow:REVISION \
  --pvc ai-model-workspace \
  --foundation-path /workspace/models/sdxl-base-1.0/REVISION \
  --foundation-digest sha256:FOUNDATION \
  --adapter watercolor /workspace/runs/RUN/image/outputs/release-a-watercolor/adapter sha256:A 1.0 \
  --adapter impressionism /workspace/runs/RUN/image/outputs/release-b-impressionism/adapter sha256:B 1.0 \
  --node-selector-key nvidia.com/gpu.product \
  --node-selector-value NVIDIA-H200 \
  --image-pull-policy Never \
  --node-local-image-id sha256:IMAGE \
  --tolerate-control-plane \
  --output evidence/image-serving.json
```

POST exactly one request instance to
`/v1/models/image-b-watercolor-impressionism:predict`. The response contains a
real SDXL PNG as base64 plus its digest, prompt, seed, and loaded model identity.
