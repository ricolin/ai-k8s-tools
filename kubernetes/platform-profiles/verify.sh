#!/usr/bin/env bash
set -euo pipefail

: "${KUBECONFIG:?set KUBECONFIG to the profile-managed workload cluster}"

kubectl_bin=${KUBECTL_BIN:-$(command -v kubectl)}
jq_bin=${JQ_BIN:-$(command -v jq)}
workload_namespace=${WORKLOAD_NAMESPACE:-ai-workflows}
workspace_pvc=${WORKSPACE_PVC:-ai-model-workspace}
storage_class=${STORAGE_CLASS:-local-path}
expected_gpu_count=${EXPECTED_GPU_COUNT:-8}

require_crd() {
  local crd
  for crd in "$@"; do
    "${kubectl_bin}" wait --for=condition=Established "crd/${crd}" --timeout=120s
  done
}

require_deployment() {
  local namespace=$1
  shift
  local deployment
  for deployment in "$@"; do
    "${kubectl_bin}" -n "${namespace}" wait --for=condition=Available \
      "deployment/${deployment}" --timeout=600s
  done
}

require_crd \
  workflows.argoproj.io \
  scheduledworkflows.kubeflow.org \
  clusterqueues.kueue.x-k8s.io \
  localqueues.kueue.x-k8s.io \
  resourceflavors.kueue.x-k8s.io \
  workloads.kueue.x-k8s.io \
  clustertrainingruntimes.trainer.kubeflow.org \
  trainingruntimes.trainer.kubeflow.org \
  trainjobs.trainer.kubeflow.org \
  inferenceservices.serving.kserve.io \
  servingruntimes.serving.kserve.io \
  clusterservingruntimes.serving.kserve.io

require_deployment cert-manager \
  cert-manager cert-manager-cainjector cert-manager-webhook
require_deployment kubeflow \
  cache-deployer-deployment cache-server metadata-envoy-deployment \
  metadata-grpc-deployment metadata-writer ml-pipeline \
  ml-pipeline-persistenceagent ml-pipeline-scheduledworkflow ml-pipeline-ui \
  ml-pipeline-viewer-crd ml-pipeline-visualizationserver mysql seaweedfs \
  workflow-controller kserve-controller-manager \
  kserve-localmodel-controller-manager llmisvc-controller-manager
require_deployment default model-registry-db model-registry-deployment
require_deployment kueue-system ai-scheduling-kueue-controller-manager
require_deployment kubeflow-system \
  jobset-controller ai-training-kubeflow-trainer-controller-manager

test "$("${kubectl_bin}" -n gpu-operator get clusterpolicy cluster-policy \
  -o jsonpath='{.status.state}')" = ready
"${kubectl_bin}" get nodes -o json | "${jq_bin}" -e \
  --argjson expected "${expected_gpu_count}" '
    ([.items[].status.capacity["nvidia.com/gpu"] // "0" | tonumber] | add) == $expected and
    ([.items[].status.allocatable["nvidia.com/gpu"] // "0" | tonumber] | add) == $expected
  '

"${kubectl_bin}" get storageclass "${storage_class}" -o json | \
  "${jq_bin}" -e '
    .provisioner == "rancher.io/local-path" and
    .volumeBindingMode == "WaitForFirstConsumer"
  '
"${kubectl_bin}" -n "${workload_namespace}" get pvc "${workspace_pvc}" \
  -o json | "${jq_bin}" -e --arg storage_class "${storage_class}" '
    .status.phase == "Bound" and
    .spec.storageClassName == $storage_class and
    .metadata.annotations["helm.sh/resource-policy"] == "keep"
  '

"${kubectl_bin}" get resourceflavor h200 >/dev/null
"${kubectl_bin}" get clusterqueue h200-ai -o json | "${jq_bin}" -e '
  .status.conditions[] | select(.type == "Active" and .status == "True")
' >/dev/null
"${kubectl_bin}" -n "${workload_namespace}" get localqueue ai-workflows \
  -o json | "${jq_bin}" -e '
    .spec.clusterQueue == "h200-ai" and
    (.status.conditions[] | select(.type == "Active" and .status == "True"))
  ' >/dev/null

echo "PASS: profile-managed AI platform is ready with ${expected_gpu_count} GPUs"
