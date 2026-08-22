# Sandboxed Repository And Pull-Request Review Agent

This follow-up reuses an accepted `code-reviewer-c` adapter. The model reviews
supplied repository or pull-request evidence, may propose one text-only unified
diff, and selects an operator-owned test profile. The broker validates all
identifiers and patch paths before Kubernetes sees the candidate.

The agent never invents or executes shell commands. Commands and digest-pinned
images live in the selected profile. The model can request patch application,
tests, patch export, and report creation through typed tools only.

## Plain-Text Entry Point

The operator may start with one string:

```bash
kubernetes/tools/ai-workflow code-agent parse-intent \
  --text 'go review https://github.com/ricolin/ai-build-tools/' \
  --output intent.json

kubernetes/tools/ai-workflow code-agent parse-intent \
  --text 'go review https://github.com/ricolin/ai-build-tools/ on the bash scripts' \
  --output intent.json

kubernetes/tools/ai-workflow code-agent parse-intent \
  --text 'go review https://github.com/ricolin/ai-build-tools/ and provide fix until all your review green' \
  --output intent.json

kubernetes/tools/ai-workflow code-agent parse-intent \
  --text 'go review https://github.com/ricolin/ai-build-tools/pull/42 on the python and yaml files' \
  --output intent.json
```

The parser canonicalizes the clone URL, selects the Bash/Python/Go/Rust/YAML
path scope, and chooses either `review-only` or `fix-until-green`. Fix mode is
capped at five iterations, retains every attempt, and sets `publish=false`.
It does not trust a moving branch: the controller must still resolve a full
40-character commit and select an operator-approved test profile before asking
the model to review anything. A pull-request URL is reduced to its base clone
URL while the immutable pull-request number remains explicit in the intent.

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
paths, parent traversal, unterminated diffs, and patches larger than 256 KiB.
One candidate patch may update both implementation and focused unit-test files
when those paths are present in the supplied evidence.

## Render The Sandbox

```bash
kubernetes/tools/ai-workflow code-sandbox \
  --source-lock source-lock.json \
  --profile python-tox.json \
  --candidate-response response.json \
  --namespace code-review-RUN_ID \
  --pvc review-workspace \
  --storage-class local-path \
  --tolerate-control-plane \
  --output sandbox
```

Use `--tolerate-control-plane` only for a cluster where the intended sandbox
node is also a control-plane node. It is disabled by default.

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

## Bounded Review-Fix-Test Loop

For `fix-until-green`, use a new retained namespace/PVC for every iteration:

1. validate the model response;
2. run `git apply --check` against the exact source lock;
3. run the selected profile in the no-egress test Job;
4. supply parser, lint, unit-test, and patch-application failures as new
   evidence to the next model request;
5. after tests pass, ask Model C to review the exact exported patch and
   observed test results;
6. stop at five candidate iterations even when the result is not green.

Evaluate the final gate with:

```bash
kubernetes/tools/ai-workflow code-agent evaluate-green \
  --response final-response.json \
  --result-env results/result.env \
  --output green-gate.json
jq -e '.status == "GREEN"' green-gate.json
```

`GREEN` requires `UNIT_TEST_STATUS=0`, a recorded 40-character source commit,
final verdict `APPROVE` or `COMMENT`, zero remaining findings, and no further
candidate fix. The selected profile defines what passed; do not describe a
focused profile as the full repository suite.

## Export The Patch And Report

Mount the sandbox PVC in a short-lived restricted reader Pod and copy these
files from `/workspace/results` into the run evidence directory:

```text
fix.patch
fix.patch.sha256
unit-tests.log
result.env
```

Accept the candidate only after all of these checks pass:

```bash
(cd results && sha256sum -c fix.patch.sha256)
grep -Fx 'UNIT_TEST_STATUS=0' results/result.env
grep -Fx "SOURCE_COMMIT=${SOURCE_COMMIT}" results/result.env

git clone --filter=blob:none --no-checkout "${REPOSITORY}" verify-checkout
git -C verify-checkout checkout --detach "${SOURCE_COMMIT}"
git -C verify-checkout apply --check "${PWD}/results/fix.patch"
test -z "$(git -C verify-checkout status --porcelain)"
```

The final `review-report.json` must combine, without rewriting, the validated
model `review`, `candidate_fix`, and `execution_plan` with the observed source
commit, patch digest, and unit-test result. The report and patch are outputs;
the workflow does not commit, push, comment, or create a pull request.

## Result Boundary

An accepted run produces a review report plus a test-passing `fix.patch`.
Applying that patch to another checkout, committing it, pushing it, or posting
it to GitHub remains an explicit operator action outside this workflow.
