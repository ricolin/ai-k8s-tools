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

## Render training and generation Jobs

```bash
./kubernetes/tools/ai-workflow image render-job \
  --name image-release-a-train \
  --namespace ai-workflows \
  --image "${CUDA_IMAGE_WORKFLOW_IMAGE}" \
  --pvc ai-model-workspace \
  --config-path /workspace/configs/image-A.json \
  --gpu-count 8 \
  --mode train \
  --node-selector-key ai-build-tools.ricolin.dev/accelerator \
  --node-selector-value nvidia-h200 \
  --output evidence/image-release-a-job.json

./kubernetes/tools/ai-workflow image render-job \
  --name image-release-a-gallery \
  --namespace ai-workflows \
  --image "${CUDA_IMAGE_WORKFLOW_IMAGE}" \
  --pvc ai-model-workspace \
  --config-path /workspace/configs/generate-A.json \
  --gpu-count 1 \
  --mode generate \
  --node-selector-key ai-build-tools.ricolin.dev/accelerator \
  --node-selector-value nvidia-h200 \
  --output evidence/image-release-a-gallery-job.json
```

Foundation, A, and B generation configs must reference the same immutable
comparison prompt manifest. The renderer and generator do not add
release-specific prompt text. Release C remains a separate optional
cross-backbone implementation and must not reuse SDXL adapter tensors.
