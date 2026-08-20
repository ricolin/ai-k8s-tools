#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
workflow_root="${root_dir}/kubernetes/workflows"
cli="${root_dir}/kubernetes/tools/ai-workflow"
adapter="${root_dir}/kubernetes/security/adapters/python-fixture-site-v1.json"
fixture="${root_dir}/kubernetes/security/fixtures/python-site"
evidence_dir=${1:-}

if [[ -z ${evidence_dir} ]]; then
  evidence_dir=$(mktemp -d /tmp/ai-build-tools-security-research.XXXXXX)
  cleanup_evidence=1
else
  mkdir -p "${evidence_dir}"
  cleanup_evidence=0
fi

site_pid=
cleanup() {
  if [[ -n ${site_pid} ]]; then
    kill "${site_pid}" 2>/dev/null || true
    wait "${site_pid}" 2>/dev/null || true
  fi
  if [[ ${cleanup_evidence} == 1 ]]; then
    rm -rf "${evidence_dir}"
  fi
}
trap cleanup EXIT INT TERM

cd "${workflow_root}"
uv sync --python 3.12 --frozen
uv run --frozen pytest -q | tee "${evidence_dir}/pytest.txt"

"${cli}" capabilities >"${evidence_dir}/capabilities.txt"
for capability in \
  research-source-lock-v1 \
  research-reports-evidence-only-v1 \
  research-novelty-gate-v1 \
  research-buildx-oci-network-none-v1 \
  research-kubernetes-runtime-render-v1 \
  research-synthetic-canary-collector-v1 \
  security-adviser-client-v1 \
  security-agent-plan-policy-v1 \
  security-model-dataset-contract-v1 \
  security-model-training-job-v1 \
  security-model-kserve-vllm-v1; do
  grep -Fx "${capability}" "${evidence_dir}/capabilities.txt" >/dev/null
done

source_dir="${evidence_dir}/source"
cp -a "${fixture}" "${source_dir}"
git -C "${source_dir}" init >/dev/null
git -C "${source_dir}" config user.name "AI Build Tools Test"
git -C "${source_dir}" config user.email "test@example.invalid"
git -C "${source_dir}" checkout -b main >/dev/null 2>&1
git -C "${source_dir}" remote add origin \
  https://example.invalid/ai-build-tools-security-fixture.git
git -C "${source_dir}" add .
git -C "${source_dir}" commit -m fixture >/dev/null

"${cli}" lock-source \
  --source "${source_dir}" \
  --repository https://example.invalid/ai-build-tools-security-fixture.git \
  --requested-ref main \
  --output "${evidence_dir}/repository-lock.json"
"${cli}" snapshot-source \
  --source "${source_dir}" \
  --output "${evidence_dir}/repository-before.json"
"${cli}" collect-source \
  --source "${source_dir}" \
  --source-lock "${evidence_dir}/repository-lock.json" \
  --output "${evidence_dir}/source-evidence.json"
"${cli}" validate-adapter \
  --adapter "${adapter}" \
  --output "${evidence_dir}/adapter-validation.json"
"${cli}" build-source-runtime \
  --source "${source_dir}" \
  --source-lock "${evidence_dir}/repository-lock.json" \
  --adapter "${adapter}" \
  --output "${evidence_dir}/build-plan"

jq -n \
  --arg image "registry.example.invalid/security-fixture@sha256:$(printf fixture | sha256sum | awk '{print $1}')" \
  --arg commit "$(git -C "${source_dir}" rev-parse HEAD)" \
  '{
    schema_version: "1.0.0",
    image: $image,
    source_commit: $commit,
    source_tree_modified: false,
    management_credentials_present: false
  }' >"${evidence_dir}/published-artifact-lock.json"

"${cli}" render-kubernetes-runtime \
  --artifact-lock "${evidence_dir}/published-artifact-lock.json" \
  --adapter "${adapter}" \
  --namespace security-research-fixture \
  --name fixture-site \
  --run-id local-verification \
  --output "${evidence_dir}/kubernetes-runtime"

port=$(uv run --frozen python - <<'PY'
import socket
with socket.socket() as stream:
    stream.bind(("127.0.0.1", 0))
    print(stream.getsockname()[1])
PY
)
PORT="${port}" uv run --frozen python "${source_dir}/site.py" \
  >"${evidence_dir}/site.log" 2>&1 &
site_pid=$!
for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -fsS "http://127.0.0.1:${port}/healthz" >"${evidence_dir}/healthz.json"

jq \
  --arg origin "http://127.0.0.1:${port}" \
  '.origin = $origin | .addresses = ["127.0.0.1"]' \
  "${evidence_dir}/kubernetes-runtime/runtime-inventory.json" \
  >"${evidence_dir}/runtime-inventory.json"

"${cli}" create-runtime-authorization \
  --repository-lock "${evidence_dir}/repository-lock.json" \
  --artifact-lock "${evidence_dir}/published-artifact-lock.json" \
  --runtime-inventory "${evidence_dir}/runtime-inventory.json" \
  --scan-mode active-safe-canary \
  --max-requests 20 \
  --requests-per-second 5 \
  --max-concurrency 1 \
  --max-seconds 30 \
  --output "${evidence_dir}/runtime-authorization.json"
"${cli}" collect-site \
  --authorization "${evidence_dir}/runtime-authorization.json" \
  --output "${evidence_dir}/site-evidence"

jq -n \
  --slurpfile proof "${evidence_dir}/site-evidence/proofs.json" \
  '{
    title: "Synthetic fixture authorization boundary",
    proof_state: "PROVEN",
    research_classification: "KNOWN_OPEN",
    confidence: "HIGH",
    observation: "The synthetic viewer reached the root-equivalent canary.",
    evidence: ["site-evidence/proofs.json#0"],
    likely_owner: "ai-build-tools fixture",
    remediation_recommendation: "Require the synthetic administrator role for the canary route.",
    regression_test_recommendation: "Require HTTP 403 for viewer-fixture and HTTP 200 only for the synthetic administrator.",
    upstream_change_authorized: false,
    public_disclosure_authorized: false,
    proof: $proof[0][0]
  }' >"${evidence_dir}/finding.json"

"${cli}" render-private-reports \
  --finding "${evidence_dir}/finding.json" \
  --output "${evidence_dir}/assessment"
"${cli}" verify-reports-evidence-only \
  --assessment "${evidence_dir}/assessment" \
  --output "${evidence_dir}/reports-only-verification.json"
"${cli}" verify-source-unchanged \
  --source "${source_dir}" \
  --before "${evidence_dir}/repository-before.json" \
  --output "${evidence_dir}/repository-after.json"

model_dir="${evidence_dir}/security-model"
mkdir -p \
  "${model_dir}/dataset" \
  "${model_dir}/foundation" \
  "${model_dir}/adapter" \
  "${model_dir}/tokenizer" \
  "${model_dir}/release"
printf 'foundation\n' >"${model_dir}/foundation/model.bin"
printf 'adapter\n' >"${model_dir}/adapter/adapter_model.safetensors"
printf 'tokenizer\n' >"${model_dir}/tokenizer/tokenizer.json"
printf '{{ messages }}\n' >"${model_dir}/release/chat-template.jinja"
printf '{}\n' >"${model_dir}/release/verification-plan.schema.json"
printf '{}\n' >"${model_dir}/release/finding.schema.json"
printf '{"analysis_only":true}\n' >"${model_dir}/release/policy-profile.json"

uv run --frozen python - "${model_dir}/dataset" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
record = {
    "id": "fixture-a-1",
    "stage": "A",
    "split": "train",
    "source": "synthetic fixture",
    "license": "CC0-1.0",
    "permission_confirmed": True,
    "target_type": "general-defense",
    "messages": [
        {"role": "user", "content": "Review the synthetic fixture."},
        {"role": "assistant", "content": "One bounded finding is supported."},
    ],
    "evidence_ids": ["fixture"],
    "allowed_operations": ["analysis"],
    "forbidden_operations": ["source-write"],
}
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
record["record_digest"] = "sha256:" + hashlib.sha256(canonical(record)).hexdigest()
records = root / "records.jsonl"
records.write_bytes(canonical(record) + b"\n")
manifest = {
    "schema_version": "1.0.0",
    "dataset_name": "security-fixture",
    "license_review_complete": True,
    "records": "records.jsonl",
    "records_digest": "sha256:" + hashlib.sha256(records.read_bytes()).hexdigest(),
    "record_count": 1,
    "stage_counts": {"A": 1, "B": 0, "C": 0},
    "split_counts": {"adversarial": 0, "hidden": 0, "train": 1, "validation": 0},
}
(root / "manifest.json").write_bytes(canonical(manifest) + b"\n")
PY

"${cli}" model validate-dataset \
  --manifest "${model_dir}/dataset/manifest.json" \
  --dataset-root "${model_dir}/dataset" \
  --output "${model_dir}/dataset-validation.json"

foundation_digest=$(uv run --frozen python - "${model_dir}/foundation" <<'PY'
import sys
from pathlib import Path
from ai_build_tools_k8s.workflow import sha256_tree
print("sha256:" + sha256_tree(Path(sys.argv[1])))
PY
)
"${cli}" model create-adviser-release \
  --foundation-digest "${foundation_digest}" \
  --adapter "${model_dir}/adapter" \
  --tokenizer "${model_dir}/tokenizer" \
  --chat-template "${model_dir}/release/chat-template.jinja" \
  --verification-plan-schema "${model_dir}/release/verification-plan.schema.json" \
  --finding-schema "${model_dir}/release/finding.schema.json" \
  --policy-profile "${model_dir}/release/policy-profile.json" \
  --lora-rank 64 \
  --output "${model_dir}/release/advisor-release.json"
"${cli}" model verify-mounted-release \
  --release "${model_dir}/release/advisor-release.json" \
  --foundation "${model_dir}/foundation" \
  --adapter "${model_dir}/adapter" \
  --tokenizer "${model_dir}/tokenizer" \
  --chat-template "${model_dir}/release/chat-template.jinja" \
  --verification-plan-schema "${model_dir}/release/verification-plan.schema.json" \
  --finding-schema "${model_dir}/release/finding.schema.json" \
  --policy-profile "${model_dir}/release/policy-profile.json" \
  --output "${model_dir}/mounted-release-verification.json"
"${cli}" model render-training-job \
  --name security-adviser-a \
  --namespace ai-workflows \
  --trainer-image "registry.example.invalid/security-trainer@sha256:$(printf trainer | sha256sum | awk '{print $1}')" \
  --pvc security-models \
  --config-path /workspace/configs/A.json \
  --gpu-count 8 \
  --node-selector-key accelerator \
  --node-selector-value h200 \
  --output "${model_dir}/training-job.json"
"${cli}" model render-adviser-serving \
  --release "${model_dir}/release/advisor-release.json" \
  --name security-adviser-c \
  --namespace ai-workflows \
  --vllm-image "registry.example.invalid/vllm@sha256:$(printf vllm | sha256sum | awk '{print $1}')" \
  --verifier-image "registry.example.invalid/verifier@sha256:$(printf verifier | sha256sum | awk '{print $1}')" \
  --pvc security-models \
  --gpu-count 1 \
  --node-selector-key accelerator \
  --node-selector-value h200 \
  --output "${model_dir}/adviser-inferenceservice.json"

adapter_digest=$(jq -r .adapter_digest "${model_dir}/release/advisor-release.json")
jq -n \
  --arg adviser_identity "${adapter_digest}" \
  '{
    adviser_identity: $adviser_identity,
    finding: {
      title: "Synthetic fixture authorization boundary",
      proof_state: "PROVEN",
      research_classification: "KNOWN_OPEN",
      evidence: ["site-evidence/proofs.json#0"],
      upstream_change_authorized: false,
      public_disclosure_authorized: false
    },
    verification_plan: {
      target_type: "upstream-research",
      research_selector: "public-source-runtime",
      analysis_only: true,
      reports_and_evidence_only: true,
      tasks: [{
        id: "draft-report",
        tool: "draft_private_finding",
        arguments: {evidence_ids: ["site-evidence/proofs.json#0"]},
        timeout_seconds: 60,
        cleanup_required: true
      }]
    }
  }' >"${model_dir}/adviser-response-fixture.json"
"${cli}" agent run \
  --release "${model_dir}/release/advisor-release.json" \
  --manifest "${evidence_dir}/runtime-authorization.json" \
  --evidence "${evidence_dir}/site-evidence/site-evidence.json" \
  --response-fixture "${model_dir}/adviser-response-fixture.json" \
  --output "${model_dir}/adviser-run"

find "${evidence_dir}" -type f ! -name SHA256SUMS -print0 \
  | sort -z | xargs -0 sha256sum >"${evidence_dir}/SHA256SUMS"
echo "PASS: research, adviser policy, model identity, training render, serving render, and reports-only gates"
