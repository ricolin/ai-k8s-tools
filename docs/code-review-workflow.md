# H200 Code-Review Model Workflow

This workflow specializes a frozen causal-language foundation into a code
reviewer for Bash, Python, Go, Rust, and YAML. KFP owns the A/B/C DAG; each
stage becomes a Kubeflow Trainer `TrainJob`, Trainer materializes JobSet, and
Kueue admits a seven-GPU request while one H200 remains available for serving.
The workflow retains a dedicated dataset, contract, evaluator, release, and
serving identity.

The packaged single-user KFP control plane creates Workflow Pods in
`kubeflow`. Keep the KFP run namespace, `workload_namespace`, LocalQueue,
ServiceAccount, and `ai-model-workspace` PVC in that same namespace. The
pipeline submission rejects a mismatch before creating a run because a KFP
task Pod cannot mount a cross-namespace PVC.

## Release Progression

| Release | Purpose |
|---|---|
| A | Review one supplied file or snippet and explain evidence-backed behavior defects. |
| B | Review a repository or pull-request diff, prioritize regressions, and identify missing tests. |
| C | Produce the same review plus a typed sandbox plan and, when justified, one bounded unified diff. |

All stages use the same JSON response shape. Release C is accepted only when
the deterministic comparison contains the exact Bash, Python, Go, Rust, YAML,
and patch-agent prompt set, has no C contract failure, and C does not score
below B.

## Generate And Validate Data

```bash
python kubernetes-CUDA/code-review/generate_dataset.py \
  --output evidence/code-review-dataset \
  --stage-a-count 96 \
  --stage-b-count 224 \
  --stage-c-count 384

kubernetes/tools/ai-workflow code-review validate-dataset \
  --manifest evidence/code-review-dataset/manifest.json \
  --dataset-root evidence/code-review-dataset \
  --output evidence/code-review-dataset/validation.json
```

The included generator is deterministic CC0 functional-validation data. A
production-quality reviewer needs a separately reviewed corpus with source and
license provenance, hidden evaluation, and repository-family holdouts.

## Build The CUDA Trainer

```bash
revision=$(git rev-parse HEAD)
kubernetes-CUDA/code-review/build-trainer-image.sh \
  "${revision}" \
  registry.example/pytorch@sha256:REPLACE \
  evidence/code-review-image \
  registry.example/ai/code-review-trainer \
  --push
```

The image reuses the domain-neutral trainer and model server already proven by
the security track. Only the engine is shared. Security datasets, contracts,
adapters, prompts, evidence, and model names remain under `kubernetes/security`
and `kubernetes-CUDA/security`.

## Train A, B, And C

Each config follows the existing offline trainer schema. A has no parent. B
uses the accepted immutable A adapter. C uses the accepted immutable B adapter.
Use a distinct output directory for every attempt.

Compile and submit the KFP package with the exact source-locked workflow image
and a JSON argument file. Set `gpu_count` to `7`; the platform contract rejects
an eight-GPU training request because one device is reserved for KServe.

```bash
kubernetes/tools/ai-workflow code-review-pipeline \
  --workflow-image registry.example/ai/workflows@sha256:REPLACE \
  --output evidence/code-review-pipeline.yaml \
  --kfp-host http://ml-pipeline.kubeflow.svc.cluster.local:8888 \
  --run-name code-review-RUN_ID \
  --arguments evidence/code-review-pipeline-arguments.json \
  --run-output evidence/code-review-pipeline-run.json \
  --namespace kubeflow \
  --service-account ai-workflow-runner
```

Use `--tolerate-control-plane` only when the selected GPU node is intentionally
also a Kubernetes control-plane node. The option defaults to disabled.

The KFP component Pods are CPU-only. Require a KFP `SUCCEEDED` state, a
Kueue-admitted Workload, completed TrainJob and JobSet identities, and
`world_size: 7` with seven unique H200 rank identities, the expected
foundation/dataset identities, and an unchanged parent digest for B and C.
The direct `render-training-job` command remains a break-glass diagnostic path;
it is not the accepted end-to-end workflow.

## Compare And Gate

Run `/opt/ai-code-review/evaluate_reviewer.py` with the frozen
`kubernetes/code-review/comparison-prompts.json`, then:

```bash
python /opt/ai-code-review/quality_gate.py \
  --responses /workspace/runs/RUN_ID/code-review/comparison/responses.jsonl \
  --output /workspace/runs/RUN_ID/code-review/comparison/quality-gate.json
```

Do not promote C when the gate fails. A rejected candidate may be served only
with `--serving-tier evaluation`; it remains explicitly promotion-blocked.

## Create And Serve Release C

Create `code-review-release.json` with `ai-code-review-model create-release`.
The release pins the foundation, adapter, tokenizer, chat template, review
schema, agent-plan schema, and policy profile. It starts at
`TRAINING_COMPLETE`. Promote immutable copies through:

```text
TRAINING_COMPLETE -> WORKFLOW_VALIDATED
WORKFLOW_VALIDATED -> QUALITY_REJECTED
WORKFLOW_VALIDATED -> SERVING_CANARY -> PRODUCTION_APPROVED
```

Every transition requires a `sha256:` evidence digest. Only
`PRODUCTION_APPROVED` can render `--serving-tier production`; only
`SERVING_CANARY` can render canary. Render production KServe with:

```bash
kubernetes/tools/ai-workflow code-review render-serving \
  --release /workspace/runs/RUN_ID/code-review/release/code-review-release.json \
  --name code-reviewer-c \
  --namespace kubeflow \
  --vllm-image registry.example/vllm@sha256:REPLACE \
  --verifier-image registry.example/ai/workflows@sha256:REPLACE \
  --pvc ai-model-workspace \
  --gpu-count 1 \
  --node-selector-key nvidia.com/gpu.product \
  --node-selector-value NVIDIA-H200 \
  --tolerate-control-plane \
  --serving-tier production \
  --output evidence/code-reviewer-c-inferenceservice.json
```

Continue with [Code-review agent workflow](code-review-agent-workflow.md) only
after mounted identity verification, KServe readiness, and one exact-contract
online response pass.
