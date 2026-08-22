#!/usr/bin/env bash
set -euo pipefail

required=(
  POD_NAME TARGET_NAMESPACE OPERATOR_NAMESPACE CLUSTER_POLICY_NAME
  DEVICE_PLUGIN_DAEMONSET EXPECTED_GPU_NODES EXPECTED_GPU_COUNT_PER_NODE
  FULL_NODE_GPU_COUNT CUDA_SMOKE_IMAGE EVIDENCE_CONFIGMAP
  STAGE_TIMEOUT_SECONDS POLL_INTERVAL_SECONDS MINIMUM_KUBERNETES_MINOR
  REQUIRE_LINUX_NODES REQUIRE_AMD64_NODES CUDA_SMOKE_IMAGE_PULL_POLICY
  SMOKE_NODE_SELECTOR_JSON SMOKE_TOLERATIONS_JSON
  SMOKE_IMAGE_PULL_SECRETS_JSON
)
for name in "${required[@]}"; do
  [[ -n ${!name:-} ]] || { echo "missing required variable: ${name}" >&2; exit 2; }
done
[[ ${CUDA_SMOKE_IMAGE} =~ @sha256:[a-f0-9]{64}$ ]] || {
  echo "CUDA_SMOKE_IMAGE is not digest-pinned" >&2
  exit 2
}

deadline=$((SECONDS + STAGE_TIMEOUT_SECONDS))
wait_until() {
  local description=$1
  shift
  while ! "$@"; do
    if (( SECONDS >= deadline )); then
      echo "timed out waiting for ${description}" >&2
      return 1
    fi
    sleep "${POLL_INTERVAL_SECONDS}"
  done
}

server_minor=$(kubectl version -o json | jq -r '.serverVersion.minor | sub("[^0-9].*$"; "")')
(( server_minor >= MINIMUM_KUBERNETES_MINOR )) || {
  echo "Kubernetes minor ${server_minor} is below required ${MINIMUM_KUBERNETES_MINOR}" >&2
  exit 1
}

nodes_json=/tmp/nodes.json
kubectl get nodes -o json >"${nodes_json}"
if [[ ${REQUIRE_LINUX_NODES} == true ]]; then
  jq -e 'all(.items[]; .metadata.labels["kubernetes.io/os"] == "linux")' "${nodes_json}" >/dev/null
fi
if [[ ${REQUIRE_AMD64_NODES} == true ]]; then
  jq -e 'all(.items[]; .metadata.labels["kubernetes.io/arch"] == "amd64")' "${nodes_json}" >/dev/null
fi

cluster_policy_ready() {
  local policy
  policy=$(kubectl get clusterpolicy.nvidia.com "${CLUSTER_POLICY_NAME}" -o json 2>/dev/null) || return 1
  jq -e '((.status.state // "") | ascii_downcase) == "ready" or any(.status.conditions[]?; .type == "Ready" and .status == "True")' \
    <<<"${policy}" >/dev/null
}
wait_until "NVIDIA ClusterPolicy Ready" cluster_policy_ready

deadline=$((SECONDS + STAGE_TIMEOUT_SECONDS))
wait_until "device-plugin DaemonSet rollout" \
  kubectl -n "${OPERATOR_NAMESPACE}" rollout status \
    "daemonset/${DEVICE_PLUGIN_DAEMONSET}" --timeout=30s

gpu_capacity_ready() {
  kubectl get nodes -o json >"${nodes_json}"
  local node_count expected_total capacity allocatable
  node_count=$(jq '[.items[] | select(((.status.capacity["nvidia.com/gpu"] // "0") | tonumber) > 0)] | length' "${nodes_json}")
  expected_total=$((EXPECTED_GPU_NODES * EXPECTED_GPU_COUNT_PER_NODE))
  capacity=$(jq '[.items[].status.capacity["nvidia.com/gpu"] // "0" | tonumber] | add // 0' "${nodes_json}")
  allocatable=$(jq '[.items[].status.allocatable["nvidia.com/gpu"] // "0" | tonumber] | add // 0' "${nodes_json}")
  [[ ${node_count} -eq ${EXPECTED_GPU_NODES} && ${capacity} -eq ${expected_total} && ${allocatable} -eq ${expected_total} ]]
}
deadline=$((SECONDS + STAGE_TIMEOUT_SECONDS))
wait_until "expected NVIDIA GPU capacity and allocatable counts" gpu_capacity_ready

run_cuda_job() {
  local name=$1 gpu_count=$2 log_file=$3
  kubectl -n "${TARGET_NAMESPACE}" delete job "${name}" --ignore-not-found --wait=true
  cat <<EOF | kubectl -n "${TARGET_NAMESPACE}" apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${name}
  labels:
    app.kubernetes.io/name: nvidia-ai-readiness-smoke
spec:
  backoffLimit: 0
  activeDeadlineSeconds: ${STAGE_TIMEOUT_SECONDS}
  ttlSecondsAfterFinished: 604800
  template:
    metadata:
      labels:
        app.kubernetes.io/name: nvidia-ai-readiness-smoke
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      nodeSelector: ${SMOKE_NODE_SELECTOR_JSON}
      tolerations: ${SMOKE_TOLERATIONS_JSON}
      imagePullSecrets: ${SMOKE_IMAGE_PULL_SECRETS_JSON}
      containers:
        - name: cuda-smoke
          image: ${CUDA_SMOKE_IMAGE}
          imagePullPolicy: ${CUDA_SMOKE_IMAGE_PULL_POLICY}
          env:
            - name: EXPECTED_GPU_COUNT
              value: "${gpu_count}"
          resources:
            limits:
              nvidia.com/gpu: "${gpu_count}"
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
EOF
  kubectl -n "${TARGET_NAMESPACE}" wait --for=condition=Complete \
    "job/${name}" --timeout="${STAGE_TIMEOUT_SECONDS}s"
  kubectl -n "${TARGET_NAMESPACE}" logs "job/${name}" | tee "${log_file}"
}

one_job=nvidia-ai-one-gpu
full_job=nvidia-ai-full-node
run_cuda_job "${one_job}" 1 /tmp/one-gpu.log
run_cuda_job "${full_job}" "${FULL_NODE_GPU_COUNT}" /tmp/full-node.log

kubectl get clusterpolicy.nvidia.com "${CLUSTER_POLICY_NAME}" -o json |
  jq '{apiVersion,kind,metadata:{name:.metadata.name,uid:.metadata.uid},status}' \
    >/tmp/cluster-policy.json
kubectl -n "${OPERATOR_NAMESPACE}" get daemonset "${DEVICE_PLUGIN_DAEMONSET}" -o json |
  jq '{apiVersion,kind,metadata:{name:.metadata.name,uid:.metadata.uid,generation:.metadata.generation},spec:{selector:.spec.selector,template:{spec:{containers:[.spec.template.spec.containers[]|{name,image}]}}},status}' \
    >/tmp/device-plugin.json
kubectl -n "${OPERATOR_NAMESPACE}" get pods -o json |
  jq '{apiVersion,kind,items:[.items[]|{metadata:{name:.metadata.name,uid:.metadata.uid},spec:{nodeName:.spec.nodeName,containers:[.spec.containers[]|{name,image}]},status:{phase:.status.phase,containerStatuses:[.status.containerStatuses[]?|{name,image,imageID,ready,restartCount}]}}]}' \
    >/tmp/operator-pods.json
kubectl -n "${TARGET_NAMESPACE}" get pod "${POD_NAME}" -o json |
  jq '{apiVersion,kind,metadata:{name:.metadata.name,uid:.metadata.uid},spec:{nodeName:.spec.nodeName,containers:[.spec.containers[]|{name,image}]},status:{phase:.status.phase,containerStatuses:[.status.containerStatuses[]?|{name,image,imageID,ready,restartCount}]}}' \
    >/tmp/readiness-pod.json
kubectl get nodes -o json |
  jq '{apiVersion,kind,items:[.items[]|{metadata:{name:.metadata.name,uid:.metadata.uid,labels:{"kubernetes.io/arch":.metadata.labels["kubernetes.io/arch"],"kubernetes.io/os":.metadata.labels["kubernetes.io/os"]}},status:{capacity:{"nvidia.com/gpu":.status.capacity["nvidia.com/gpu"]},allocatable:{"nvidia.com/gpu":.status.allocatable["nvidia.com/gpu"]},nodeInfo:{architecture:.status.nodeInfo.architecture,containerRuntimeVersion:.status.nodeInfo.containerRuntimeVersion,kernelVersion:.status.nodeInfo.kernelVersion,kubeletVersion:.status.nodeInfo.kubeletVersion,operatingSystem:.status.nodeInfo.operatingSystem,osImage:.status.nodeInfo.osImage}}}]}' \
    >"${nodes_json}"
jq -n \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg orchestrator_image "${ORCHESTRATOR_IMAGE_ID:-unknown}" \
  --arg cuda_image "${CUDA_SMOKE_IMAGE}" \
  --argjson expected_nodes "${EXPECTED_GPU_NODES}" \
  --argjson expected_per_node "${EXPECTED_GPU_COUNT_PER_NODE}" \
  --argjson full_node_count "${FULL_NODE_GPU_COUNT}" \
  --slurpfile nodes "${nodes_json}" \
  --slurpfile policy /tmp/cluster-policy.json \
  --slurpfile plugin /tmp/device-plugin.json \
  --slurpfile operator_pods /tmp/operator-pods.json \
  --slurpfile readiness_pod /tmp/readiness-pod.json \
  --rawfile one_gpu /tmp/one-gpu.log \
  --rawfile full_node /tmp/full-node.log \
  '{schema_version:"1.0.0", status:"PASS", generated_at:$generated_at,
    orchestrator_image:$orchestrator_image, cuda_smoke_image:$cuda_image,
    expected:{gpu_nodes:$expected_nodes,gpus_per_node:$expected_per_node,full_node_gpus:$full_node_count},
    nodes:$nodes[0], cluster_policy:$policy[0], device_plugin:$plugin[0],
    operator_pods:$operator_pods[0], readiness_pod:$readiness_pod[0],
    one_gpu_log:$one_gpu, full_node_log:$full_node}' >/tmp/evidence.json

evidence_configmap=/tmp/evidence-configmap.json
kubectl -n "${TARGET_NAMESPACE}" create configmap "${EVIDENCE_CONFIGMAP}" \
  --from-file=evidence.json=/tmp/evidence.json \
  --dry-run=client -o json >"${evidence_configmap}"
if kubectl -n "${TARGET_NAMESPACE}" get configmap "${EVIDENCE_CONFIGMAP}" >/dev/null 2>&1; then
  kubectl -n "${TARGET_NAMESPACE}" replace -f "${evidence_configmap}"
else
  kubectl -n "${TARGET_NAMESPACE}" create -f "${evidence_configmap}"
fi
kubectl -n "${TARGET_NAMESPACE}" get configmap "${EVIDENCE_CONFIGMAP}" -o json >/tmp/evidence-configmap.json
jq -e '.data["evidence.json"] | fromjson | .status == "PASS"' /tmp/evidence-configmap.json >/dev/null
echo "PASS: ordered NVIDIA one-GPU and full-node readiness completed"
