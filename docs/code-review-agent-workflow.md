# Sandboxed Repository And Pull-Request Review Agent

This follow-up reuses an accepted `code-reviewer-c` adapter. The model reviews
supplied repository or pull-request evidence, may propose one text-only unified
diff, and selects an operator-owned test profile. The broker validates all
identifiers and patch paths before Kubernetes sees the candidate.

The agent never invents or executes shell commands. Commands and digest-pinned
images live in the selected profile. The model can request patch application,
tests, patch export, and report creation through typed tools only.

## Inputs

Prepare:

1. `code-review-release.json` from the accepted C adapter.
2. A source lock with `id`, public HTTPS `repository`, and exact 40-character `commit`.
3. Optional pull-request lock with immutable base/head commits and a captured diff digest.
4. A review packet containing source/diff evidence and a complete reference index.
5. One or more operator-reviewed test profiles using digest-pinned fetch and runner images.

Generate the model request instead of hand-writing it:

```bash
kubernetes/tools/ai-workflow code-agent create-request \
  --release code-review-release.json \
  --packet review-packet.json \
  --output request.json
```

After calling the OpenAI-compatible `code-reviewer-c` endpoint, validate the
exact response:

```bash
kubernetes/tools/ai-workflow code-agent validate-response \
  --release code-review-release.json \
  --packet review-packet.json \
  --response response.json
```

Validation rejects invented evidence/profile/repository IDs, unknown tools,
arbitrary command fields, binary patches, renames, absolute paths, `.git`
paths, parent traversal, and patches larger than 256 KiB.

## Render The Sandbox

```bash
kubernetes/tools/ai-workflow code-sandbox \
  --source-lock source-lock.json \
  --profile python-tox.json \
  --candidate-response response.json \
  --namespace code-review-RUN_ID \
  --pvc review-workspace \
  --storage-class local-path \
  --output sandbox
```

The bundle contains a restricted namespace, RWO workspace PVC, default-deny
network policies, a pinned checkout Job, profile preparation Job, offline
patch-and-test Job, and result contract.

## Execute Linearly

```bash
kubectl apply -f sandbox/namespace.json
kubectl apply -f sandbox/default-deny.json
kubectl apply -f sandbox/fetch-egress.json
kubectl apply -f sandbox/pvc.json
kubectl apply -f sandbox/fetch-script.json
kubectl apply -f sandbox/prepare-script.json
kubectl apply -f sandbox/test-script.json

kubectl apply -f sandbox/fetch-job.json
kubectl wait -n code-review-RUN_ID --for=condition=Complete job/review-fetch-PROFILE --timeout=1h

kubectl apply -f sandbox/prepare-job.json
kubectl wait -n code-review-RUN_ID --for=condition=Complete job/review-prepare-PROFILE --timeout=1h

kubectl apply -f sandbox/test-job.json
kubectl wait -n code-review-RUN_ID --for=condition=Complete job/review-test-PROFILE --timeout=1h
kubectl logs -n code-review-RUN_ID job/review-test-PROFILE
```

The test Job verifies a clean pinned checkout, applies the candidate with
`git apply --check`, runs `git diff --check`, executes every operator-defined
unit-test command without egress, and writes `fix.patch`, its digest,
`unit-tests.log`, and `result.env` to the PVC. A failed Job is evidence that
the candidate is not accepted; retain its logs and request a new model attempt
against those supplied results.

## Result Boundary

An accepted run produces a review report plus a test-passing `fix.patch`.
Applying that patch to another checkout, committing it, pushing it, or posting
it to GitHub remains an explicit operator action outside this workflow.

The earlier security-adviser and security-research workflows remain available
in [security-agent-workflow.md](security-agent-workflow.md). They use different
model identities, datasets, contracts, evidence, and agents.
