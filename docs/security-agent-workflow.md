# Grounded Security-Agent Workflow

## Purpose And Ownership Boundary

This guide covers the reusable, provider-neutral part of the released adviser
and security-agent workflow. The repository owns:

- immutable adviser release validation;
- mounted foundation, adapter, tokenizer, and contract verification;
- typed analysis-manifest and evidence-reference validation;
- selector-specific tool allowlists;
- fail-closed rejection of ungrounded or prohibited task arguments;
- read-only source/runtime evidence contracts; and
- evidence-only private report rendering.

The operator owns the Kubernetes cluster, KServe endpoint, model weights,
scanner containers, target authorization, registry access, and evidence
retention. A scanner result is not proof of exploitability, and a valid model
plan is not permission to execute it.

## 1. Freeze And Validate The Checkout

```bash
git clone git@github.com:ricolin/ai-k8s-tools.git
cd ai-k8s-tools
git switch --detach REVIEWED_COMMIT
test -z "$(git status --porcelain)"

uv sync --project kubernetes/workflows --python 3.12 --frozen
uv run --project kubernetes/workflows --frozen \
  pytest -q kubernetes/workflows/tests
bash -n kubernetes/tools/*.sh kubernetes/mcapi/*.sh \
  kubernetes/platform/*.sh kubernetes-CUDA/image/*.sh \
  kubernetes-CUDA/security/*.sh scripts/*.sh
```

Expected result: pytest reports no failures, shell syntax passes, and
`git status --porcelain` remains empty. Record the commit and complete output.

Verify the wrapper rather than guessing a command:

```bash
./kubernetes/tools/ai-workflow --help
./kubernetes/tools/ai-workflow capabilities
./kubernetes/tools/ai-workflow agent --help
./kubernetes/tools/ai-workflow research --help
```

Expected capabilities include `security-adviser-client-v1`,
`security-agent-plan-policy-v1`, `research-source-lock-v1`, and
`research-reports-evidence-only-v1`.

## 2. Validate The Accepted Adviser Release

Create `advisor-release.json` only after deterministic model-quality gates
accept Release C. Then validate both the manifest and mounted bytes:

```bash
export AI_WORKFLOW=$PWD/kubernetes/tools/ai-workflow
export RELEASE=/absolute/path/advisor-release.json

"${AI_WORKFLOW}" agent validate-release --release "${RELEASE}"

"${AI_WORKFLOW}" model verify-mounted-release \
  --release "${RELEASE}" \
  --foundation /absolute/path/foundation \
  --adapter /absolute/path/adapter \
  --tokenizer /absolute/path/tokenizer \
  --chat-template /absolute/path/chat-template.jinja \
  --verification-plan-schema /absolute/path/verification-plan.schema.json \
  --finding-schema /absolute/path/finding.schema.json \
  --policy-profile /absolute/path/policy-profile.json \
  --output evidence/mounted-release-verification.json

jq -e '.status == "PASS"' evidence/mounted-release-verification.json
```

Expected result: `PASS`, with foundation and adapter digests equal to the
release manifest. A mismatch must stop serving and agent execution.

## 3. Verify The KServe Endpoint

Deploy the accepted release through the site's reviewed KServe procedure.
The endpoint must expose health, model identity, and deterministic chat
completion routes:

```bash
kubectl wait -n MODEL_NAMESPACE --for=condition=Ready --timeout=15m \
  inferenceservice/MODEL_NAME
kubectl -n MODEL_NAMESPACE port-forward \
  service/MODEL_NAME-predictor 18080:80 --address 127.0.0.1
```

From a second terminal:

```bash
curl -fsS http://127.0.0.1:18080/health | jq .
curl -fsS http://127.0.0.1:18080/v1/models | jq .
```

Expected result: readiness is `True`; health returns `ready: true` and the
exact accepted foundation/adapter digests; the model list contains only the
accepted serving model. Restart the predictor and repeat these checks before
claiming restart stability.

## 4. Create A Grounded Evidence Packet

Every identifier that the model may cite must be supplied by the caller in a
complete `reference_index`. Empty categories remain present as empty arrays:

```json
{
  "reference_index": {
    "analyzer_profile_ids": [],
    "authorization_ids": ["authorized-target-1"],
    "evidence_ids": ["evidence:item-1"],
    "finding_ids": [],
    "matrix_profile_ids": [],
    "query_ids": [],
    "repository_lock_ids": [],
    "reproduction_profile_ids": ["replay-1"],
    "source_lock_ids": [],
    "target_lock_ids": [],
    "test_profile_ids": []
  },
  "observations": [{"id": "evidence:item-1", "status": "observed"}]
}
```

Create and validate an analysis manifest with one selector:

```bash
"${AI_WORKFLOW}" research create-analysis-manifest \
  --selector public-source-runtime \
  --output evidence/analysis-manifest.json
"${AI_WORKFLOW}" research validate-analysis-manifest \
  --manifest evidence/analysis-manifest.json
```

Selectors are intentionally separate: `public-image`,
`public-source-repository`, and `public-source-runtime` expose different tool
allowlists. Do not combine authorities by editing a generated manifest.

## 5. Run The Adviser And Policy Broker

```bash
"${AI_WORKFLOW}" agent run \
  --release "${RELEASE}" \
  --manifest evidence/analysis-manifest.json \
  --evidence evidence/evidence-packet.json \
  --output evidence/adviser-run \
  --endpoint http://127.0.0.1:18080 \
  --timeout 600

jq '{finding,verification_plan}' \
  evidence/adviser-run/adviser-response.json
```

Expected result:

- `adviser_identity` equals the accepted adapter digest;
- `analysis_only` and `reports_and_evidence_only` are `true`;
- every task tool is allowed for the selected evidence class;
- every task has a bounded timeout and `cleanup_required: true`; and
- every cited ID exists in `reference_index`.

The broker validates a plan; it does not automatically execute tasks.

## 6. Prove Fail-Closed Grounding

Copy the response into a new negative-test directory, replace one cited ID
with `invented:evidence`, and validate it without altering the accepted run:

```bash
"${AI_WORKFLOW}" agent run \
  --release "${RELEASE}" \
  --manifest evidence/analysis-manifest.json \
  --evidence evidence/evidence-packet.json \
  --response-fixture evidence/negative/adviser-response.json \
  --output evidence/negative/run
```

Expected result: nonzero exit status and an error such as
`ungrounded evidence_ids` or `finding cites ungrounded evidence`. A response
that fails this gate must never be repaired silently.

## 7. Render Evidence-Only Reports

```bash
jq '.finding' evidence/adviser-run/adviser-response.json \
  >evidence/accepted-finding.json

"${AI_WORKFLOW}" research render-private-reports \
  --finding evidence/accepted-finding.json \
  --output evidence/assessment

"${AI_WORKFLOW}" research verify-reports-evidence-only \
  --assessment evidence/assessment \
  --output evidence/evidence-only-verification.json

jq -e '.pass == true and (.prohibited_artifacts | length) == 0' \
  evidence/evidence-only-verification.json
```

Expected result: the verification expression returns `true`. The assessment
contains reports and evidence only, with no patch, branch, commit, issue, pull
request, publication, credential, or persistence artifact.

## 8. Supported Versus External Steps

| Function | Current state |
|---|---|
| Release identity and mounted-byte validation | Implemented |
| KServe manifest rendering contract | Implemented; deployment is site-owned |
| Grounded live/frozen adviser response | Implemented |
| Source lock, runtime authorization, safe collector, private reports | Implemented |
| Trivy, ZAP, or another scanner | External collector; freeze its image/database and ingest evidence |
| Arbitrary public-repository build adapters | Adapter framework exists; each project requires review and validation |
| Automatic plan execution | Intentionally absent |
| Source changes, issues, pull requests, or public disclosure | Intentionally prohibited |

Use [troubleshooting](troubleshooting.md) for failed gates. Site-specific
cluster creation, images, paths, and retained results belong in the private
environment operations guide.
