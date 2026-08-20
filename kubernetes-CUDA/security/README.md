# H200 security adviser CUDA backend

This directory implements the real, offline Qwen3 adviser trainer used by the
provider-neutral contracts in `kubernetes/workflows`. It does not include a
model, dataset, registry credential, kubeconfig, or site-specific endpoint.

## Runtime contract

- The foundation, tokenizer, dataset, parent adapter, and output workspace are
  mounted from a site-owned PVC.
- Every input is verified by SHA-256 before training.
- Release A starts from the frozen foundation. Release B initializes from an
  immutable copy of A; Release C initializes from B.
- The parent adapter is hashed before and after training and must be unchanged.
- `torchrun` creates exactly one process per allocated GPU. The configured and
  observed world sizes must match.
- Hugging Face and Transformers offline modes are mandatory.
- Only the final assistant response is included in the loss; prompt tokens are
  masked.
- The output is a candidate LoRA adapter. Deterministic evidence-grounding
  evaluation happens before a Release C `advisor-release.json` is created.
  A completed C training Job may still be rejected and must then remain
  disconnected from serving and agent workflows.

## Build the trainer image

Select a reviewed PyTorch CUDA base and resolve it to a digest. Generate the
build plan first:

```bash
revision=$(git rev-parse HEAD)
base=registry.example/pytorch@sha256:REPLACE
out=evidence/build-security-trainer-${revision:0:12}

./kubernetes-CUDA/security/build-trainer-image.sh \
  "${revision}" "${base}" "${out}" registry.example/ai/security-trainer
```

Review `build-command.sh`, then execute the immutable registry build:

```bash
./kubernetes-CUDA/security/build-trainer-image.sh \
  "${revision}" "${base}" "${out}" registry.example/ai/security-trainer --push
. "${out}/image.env"
```

The accepted `SECURITY_TRAINER_IMAGE` is a repository digest, not the mutable
build tag.

## Prepare A, B, and C configs

Copy `training-config.example.json` into the mounted workspace. For B and C,
set both parent fields and include replay stages explicitly:

```json
{
  "stage": "B",
  "parent_adapter_path": "/workspace/runs/accepted-A/adapter",
  "parent_adapter_digest": "sha256:...",
  "training_stages": ["A", "B"]
}
```

C normally uses `["A", "B", "C"]`. Dataset weighting must already be
resolved into the immutable JSONL epoch plan; the trainer does not invent or
silently alter replay ratios.

## Render the eight-GPU Job

```bash
./kubernetes/tools/ai-workflow model render-training-job \
  --name security-adviser-a \
  --namespace ai-workflows \
  --trainer-image "${SECURITY_TRAINER_IMAGE}" \
  --pvc security-model-workspace \
  --config-path /workspace/configs/A.json \
  --gpu-count 8 \
  --node-selector-key ai-build-tools.ricolin.dev/accelerator \
  --node-selector-value nvidia-h200 \
  --output evidence/security-adviser-a-job.json
```

Apply only after `kubectl apply --dry-run=server` accepts the manifest and all
eight GPUs are allocatable.

## Serve accepted C

Use a digest-pinned vLLM image that supports Qwen3 and PEFT LoRA. The renderer
sets `--max-lora-rank` from the immutable release, mounts the model PVC
read-only, and runs an identity verifier before vLLM starts:

```bash
./kubernetes/tools/ai-workflow model render-adviser-serving \
  --release /workspace/releases/C/advisor-release.json \
  --name security-adviser-c \
  --namespace ai-workflows \
  --vllm-image registry.example/vllm@sha256:REPLACE \
  --verifier-image registry.example/ai/workflow@sha256:REPLACE \
  --pvc security-model-workspace \
  --gpu-count 1 \
  --node-selector-key ai-build-tools.ricolin.dev/accelerator \
  --node-selector-value nvidia-h200 \
  --output evidence/security-adviser-c-inferenceservice.json
```

This directory can be syntax- and contract-tested without a GPU. A physical
claim requires the observed Kubernetes Job, rank/device map, optimizer result,
adapter identity, KServe readiness, and inference evidence from the H200 run.
