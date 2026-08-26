# Troubleshooting

## Triage Rule

Capture evidence before changing state. Retry with a new output directory or
run ID; never overwrite an accepted or failed run. First classify the failure
as cluster, GPU, model identity, KServe, evidence contract, target
authorization, external scanner, or retention.

## Quick Matrix

| Symptom | Check | Expected | Corrective action |
|---|---|---|---|
| Wrapper shows only research commands | `ai-workflow --help` | Wrapper command groups | Use a checkout containing the wrapper-help fix; use `agent`, `model`, `image`, or `research` explicitly. |
| Tests import stale code | `pwd`; `uv run --project kubernetes/workflows python -c 'import ai_build_tools_k8s; print(ai_build_tools_k8s.__file__)'` | Path under current checkout | Remove an activated unrelated virtualenv and rerun `uv sync --frozen`. |
| GPU Job is Pending | `kubectl describe pod`; inspect allocatable GPU | Requested GPU count is allocatable and selector matches | Correct selector/taint/runtime; do not lower the request and still claim the original gate. |
| CUDA unavailable | Pod logs and `nvidia.com/gpu` allocation | CUDA operation succeeds inside the allocated Pod | Repair driver, toolkit, containerd runtime, or device plugin in a maintenance window. |
| KServe never becomes Ready | `kubectl describe isvc`; predictor events/logs | Predictor Ready and digest-verification init succeeds | Check PVC paths, image pull, memory/GPU request, probes, and exact model digests. |
| Platform install stops at `deployment/mysql` | `kubectl get pvc -A`; local-path helper Pod events; provisioner logs | Storage preflight passes before KFP is applied | Use the current installer with its pinned helper image and temporary PVC probe; do not install downstream CRDs by hand. |
| `metadata-envoy` reports `Too many open files` | Node CPU count, Deployment args, previous container log | Args include `--concurrency 4` and the replacement Pod remains Ready | Re-run the current idempotent platform installer so it applies the bounded concurrency patch. |
| API rejects `spec.containers[0].image: Required value` | Inspect the rendered YAML and the image variable used by its template | Every `image:` has an immutable or intentionally node-local value | Complete the image build/preload step, persist its output, then run `validate-rendered-manifest.sh` and a server dry run. Do not substitute an unrelated smoke image. |
| Hugging Face/Xet download raises `Permission denied` | Check `HF_HOME`, `HF_HUB_CACHE`, `HF_XET_CACHE`, mounted-path ownership, and the failed Pod log | The non-root workload UID can write all three cache paths | Precreate cache directories for the workload UID and resume the same local directory. Do not treat `config.json` as snapshot completion; require the expected weight shards and a marker written outside the hashed payload tree after a successful download. Relocate timestamped local metadata before hashing. |
| Port-forward disconnects | Check predictor Pod UID | Forward targets current Pod/Service | Restart the forward after a rollout; a Pod replacement invalidates an old session. |
| Adapter/foundation mismatch | `/health` and mounted verification JSON | Digests exactly equal release manifest | Stop. Mount the accepted immutable artifacts; never edit the release manifest to match unexpected bytes. |
| Code-review Job starts the wrong trainer | Inspect the rendered container args | Code review uses `/opt/ai-code-review/trainer.py` | Invoke `ai-workflow code-review` and preserve the failed Job log. |
| Finding label appears in `finding.evidence` | Compare response with packet `evidence_ids` | `finding.id` is reviewer-created; `finding.evidence` exactly copies a supplied evidence ID | Reject the response and continue training or re-query with the identifier rules intact. Do not rewrite model output. |
| Review fetch retry reports an invalid working directory | Failed clone left an empty target and the process removed its own `WORKDIR` | Fetch starts from `/tmp`, accepts only an empty retry target, and verifies the exact commit | Use the current retry-safe fetch script; never remove a non-empty source tree. |
| Tox 4 rejects `--no-recreate` as ambiguous | Test profile depends on a version-sensitive legacy flag | Prepare all dependencies with egress, then call the prepared interpreter/test/lint binaries directly without egress | Update the operator-owned profile; do not grant test-stage egress. |
| Test runner reports `break` outside a loop | Generated sequential command script used an invalid control statement | Later commands are status-guarded and result artifacts are still exported | Use the current sandbox generator and preserve the failed attempt. |
| Exported patch checksum references `/workspace/results` | Digest recorded an in-Pod absolute path | `fix.patch.sha256` names only `fix.patch` and verifies after export | Generate the digest from inside the result directory. |
| Functional unit test passes but repository lint fails | Selected profile proved behavior but exposed a separate style defect | Candidate remains rejected and the lint output becomes next-iteration evidence | Keep all selected functional and style gates in the no-egress profile. |
| `git apply` reports a corrupt final patch line | Unified diff lacks a terminal newline or has invalid hunk counts | Broker rejects unterminated diffs and locked-checkout preflight passes | Ask for a new model emission; never repair model output in place. |
| Model returns semantic approval but broker rejects bookkeeping | Verdict and typed response contract are separate gates | Final response has valid `NOT_NEEDED`, task arguments, cleanup flags, and zero findings | Request a contract-correction response and retain both attempts. |
| `reference_index fields are incomplete` | `jq '.reference_index | keys'` | All eleven categories exist | Add missing categories as arrays, even when empty. |
| `ungrounded ...` | Compare task/finding IDs to `reference_index` | Every model-created reference is caller supplied | Reject and retain the response. Do not add invented IDs merely to pass validation. |
| Tool not allowed for selector | Inspect manifest selector and task tool | Tool belongs to selector allowlist | Split the analysis into separate manifests; do not broaden authority. |
| Prohibited argument such as `command` | Inspect task arguments | Only typed argument keys | Reject the response. Arbitrary shell execution is intentionally unsupported. |
| Collector reaches another origin | Collector request log | Off-origin count is zero | Stop the target, preserve logs, correct runtime authorization and NetworkPolicy, then use a new run. |
| Evidence checksum fails | `sha256sum -c SHA256SUMS` | Every retained object reports `OK` | Treat the bundle as modified; recover from the authoritative copy or create a new manifest with an explicit reason. |

## KServe Evidence Bundle

Collect these before changing a failed predictor:

```bash
kubectl get inferenceservice -A -o yaml >inferenceservices.yaml
kubectl get pods -A -o wide >pods.txt
kubectl get events -A --sort-by=.metadata.creationTimestamp >events.txt
kubectl logs -n MODEL_NAMESPACE deployment/MODEL_NAME-predictor \
  --all-containers >predictor.log 2>&1 || true
```

For restart testing, record the old Pod name/UID, restart or replace the
predictor through the approved controller, wait for readiness, record the new
UID, and repeat `/health` and `/v1/models`. A changed UID plus matching digests
is the minimum restart proof.

## Cleanup And Escalation

Retain completed Jobs and definitions only when the environment policy allows
it. Escalate with the immutable run ID, exact source/model/image digests,
failed command, exit code, relevant logs, observed versus expected result, and
checksum status. Never include kubeconfigs, bearer tokens, registry passwords,
or private model credentials in a portable report.
