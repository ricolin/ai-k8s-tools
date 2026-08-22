# H200 Code-Review Model Workflow

This workflow specializes a frozen causal-language foundation into a code
reviewer for Bash, Python, Go, Rust, and YAML. It reuses the proven offline
eight-GPU LoRA engine while keeping its dataset, contracts, evaluator, release,
and serving identity separate from the retained security-adviser workflow.

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

```bash
kubernetes/tools/ai-workflow code-review render-training-job \
  --name code-reviewer-a-RUN_ID \
  --namespace ai-workflows \
  --trainer-image registry.example/ai/code-review-trainer@sha256:REPLACE \
  --pvc ai-model-workspace \
  --config-path /workspace/runs/RUN_ID/code-review/configs/release-a.json \
  --gpu-count 8 \
  --node-selector-key ai-build-tools.ricolin.dev/accelerator \
  --node-selector-value nvidia-h200 \
  --output evidence/release-a-job.json
```

Apply only after `kubectl apply --dry-run=server` succeeds. Require
`world_size: 8`, eight unique H200 rank identities, the expected
foundation/dataset identities, and an unchanged parent digest for B and C.

## Compare And Gate

Run `/opt/ai-code-review/evaluate_reviewer.py` with the frozen
`kubernetes/code-review/comparison-prompts.json`, then:

```bash
python /opt/ai-code-review/quality_gate.py \
  --responses /workspace/runs/RUN_ID/code-review/comparison/responses.jsonl \
  --output /workspace/runs/RUN_ID/code-review/comparison/quality-gate.json
```

Do not export or serve C unless the gate reports `PASS`.

## Create And Serve Release C

Create `code-review-release.json` with `ai-code-review-model create-release`.
The release pins the foundation, adapter, tokenizer, chat template, review
schema, agent-plan schema, and policy profile. Render KServe with:

```bash
kubernetes/tools/ai-workflow code-review render-serving \
  --release /workspace/runs/RUN_ID/code-review/release/code-review-release.json \
  --name code-reviewer-c \
  --namespace ai-workflows \
  --vllm-image registry.example/vllm@sha256:REPLACE \
  --verifier-image registry.example/ai/workflows@sha256:REPLACE \
  --pvc ai-model-workspace \
  --gpu-count 1 \
  --node-selector-key ai-build-tools.ricolin.dev/accelerator \
  --node-selector-value nvidia-h200 \
  --output evidence/code-reviewer-c-inferenceservice.json
```

Continue with [Code-review agent workflow](code-review-agent-workflow.md) only
after mounted identity verification, KServe readiness, and one exact-contract
online response pass.
