#!/usr/bin/env bash
set -euo pipefail

: "${KUBECONFIG:?set KUBECONFIG to the target Kubernetes cluster}"
kubectl_bin=${KUBECTL_BIN:-/opt/ai-build-tools-bin/kubectl}
[[ -x ${kubectl_bin} ]] || kubectl_bin=$(command -v kubectl)

${kubectl_bin} get nodes -o wide
${kubectl_bin} get storageclass
${kubectl_bin} get pvc -A
${kubectl_bin} -n kubeflow get deployment,pod,service -o wide
${kubectl_bin} -n cert-manager get deployment,pod -o wide
${kubectl_bin} -n ai-workflows get serviceaccount,role,rolebinding,secret
${kubectl_bin} get crd workflows.argoproj.io scheduledworkflows.kubeflow.org \
  experiments.kubeflow.org trials.kubeflow.org suggestions.kubeflow.org \
  inferenceservices.serving.kserve.io servingruntimes.serving.kserve.io \
  clusterservingruntimes.serving.kserve.io

for deployment in mysql seaweedfs ml-pipeline ml-pipeline-ui model-registry-db \
  model-registry-deployment katib-controller katib-db-manager katib-mysql \
  katib-ui kserve-controller-manager; do
  ${kubectl_bin} -n kubeflow wait --for=condition=Available "deployment/${deployment}" --timeout=60s
done

if ${kubectl_bin} get nodes -o json | jq -e '.items[].status.allocatable["nvidia.com/gpu"]' >/dev/null; then
  echo "NVIDIA resources are advertised; validate the selected physical profile separately"
else
  echo "PASS: platform ready for mechanics workflows; no NVIDIA resource claim"
fi
