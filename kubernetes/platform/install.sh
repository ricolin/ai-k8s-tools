#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source "${root_dir}/kubernetes/versions.env"

[[ ${LOCAL_PATH_HELPER_IMAGE} =~ ^.+@sha256:[a-f0-9]{64}$ ]] || {
  echo "LOCAL_PATH_HELPER_IMAGE must be digest-pinned" >&2
  exit 1
}

: "${KUBECONFIG:?set KUBECONFIG to the target Kubernetes cluster}"
profile_file=${PROFILE_FILE:-${root_dir}/kubernetes/profiles/kubernetes-fixture.env}
source "${profile_file}"

source_root=${SOURCE_ROOT:-/opt/ai-build-tools-sources}
tool_root=${TOOL_ROOT:-/opt/ai-build-tools-bin}
evidence_dir=${EVIDENCE_DIR:-${root_dir}/evidence/platform}
mkdir -p "${source_root}" "${tool_root}" "${evidence_dir}"

if [[ ! -x ${tool_root}/kubectl ]]; then
  curl -fL --retry 5 -o "${tool_root}/kubectl" \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
  curl -fL --retry 5 -o "${evidence_dir}/kubectl.sha256.expected" \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256"
  printf '%s  %s\n' "$(<"${evidence_dir}/kubectl.sha256.expected")" "${tool_root}/kubectl" |
    sha256sum --check
  chmod 0755 "${tool_root}/kubectl"
fi

if [[ ! -x ${tool_root}/kustomize ]]; then
  archive="kustomize_${KUSTOMIZE_VERSION}_linux_amd64.tar.gz"
  curl -fL --retry 5 -o "${evidence_dir}/${archive}" \
    "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/${archive}"
  sha256sum "${evidence_dir}/${archive}" >"${evidence_dir}/${archive}.sha256"
  tar -xzf "${evidence_dir}/${archive}" -C "${tool_root}" kustomize
  chmod 0755 "${tool_root}/kustomize"
fi

kubectl=${tool_root}/kubectl
kustomize=${tool_root}/kustomize
server_minor=$(${kubectl} version -o json | jq -r '.serverVersion.minor | sub("[^0-9].*$"; "")')
if (( server_minor < 34 )); then
  echo "Kubeflow ${KUBEFLOW_DISTRIBUTION_REF} profile requires the validated Kubernetes 1.34+ baseline" >&2
  exit 1
fi
${kubectl} version -o json >"${evidence_dir}/kubernetes-version.json"
${kustomize} version >"${evidence_dir}/kustomize-version.txt"

if [[ ${SINGLE_NODE_CONTROL_PLANE:-false} == true && \
      ${ALLOW_CONTROL_PLANE_SCHEDULING:-false} == true ]]; then
  [[ $(${kubectl} get nodes -o name | wc -l) -eq 1 ]] || {
    echo "control-plane untaint is restricted to a one-node cluster" >&2
    exit 1
  }
  ${kubectl} get nodes -o yaml >"${evidence_dir}/nodes-before-scheduling-change.yaml"
  ${kubectl} taint nodes --all node-role.kubernetes.io/control-plane- || true
  ${kubectl} taint nodes --all node-role.kubernetes.io/master- || true
  ${kubectl} get nodes -o yaml >"${evidence_dir}/nodes-after-scheduling-change.yaml"
fi

source_dir=${source_root}/kubeflow-community-${KUBEFLOW_DISTRIBUTION_COMMIT}
if [[ ! -d ${source_dir}/.git ]]; then
  git clone --filter=blob:none --no-checkout https://github.com/kubeflow/community-distribution.git "${source_dir}"
  git -C "${source_dir}" fetch --depth 1 origin "${KUBEFLOW_DISTRIBUTION_COMMIT}"
  git -C "${source_dir}" checkout --detach "${KUBEFLOW_DISTRIBUTION_COMMIT}"
fi
observed_commit=$(git -C "${source_dir}" rev-parse HEAD)
[[ ${observed_commit} == "${KUBEFLOW_DISTRIBUTION_COMMIT}" ]] || {
  echo "unexpected Kubeflow source commit: ${observed_commit}" >&2
  exit 1
}
printf '%s\n' "${observed_commit}" >"${evidence_dir}/kubeflow-source.commit"

if ! ${kubectl} get storageclass "${STORAGE_CLASS}" >/dev/null 2>&1; then
  [[ ${STORAGE_CLASS} == local-path ]] || {
    echo "storage class does not exist: ${STORAGE_CLASS}" >&2
    exit 1
  }
  ${kubectl} apply -f \
    "https://raw.githubusercontent.com/rancher/local-path-provisioner/${LOCAL_PATH_PROVISIONER_VERSION}/deploy/local-path-storage.yaml"
fi
${kubectl} annotate storageclass "${STORAGE_CLASS}" \
  storageclass.kubernetes.io/is-default-class=true --overwrite
storage_provisioner=$(${kubectl} get storageclass "${STORAGE_CLASS}" \
  -o jsonpath='{.provisioner}')

if [[ ${storage_provisioner} == rancher.io/local-path ]]; then
  ${kubectl} -n local-path-storage rollout status \
    deployment/local-path-provisioner --timeout=300s

  # The upstream manifest uses mutable busybox:latest for helper Pods. Pin it
  # before Kubeflow queues large control-plane image pulls on a one-node cluster.
  helper_pod_yaml=$(${kubectl} -n local-path-storage get configmap local-path-config \
    -o jsonpath='{.data.helperPod\.yaml}')
  helper_pod_yaml=$(printf '%s\n' "${helper_pod_yaml}" |
    sed -E "s#(^[[:space:]]*image:[[:space:]]*).*busybox[^[:space:]]*#\\1${LOCAL_PATH_HELPER_IMAGE}#")
  grep -Fq "image: ${LOCAL_PATH_HELPER_IMAGE}" <<<"${helper_pod_yaml}" || {
    echo "failed to pin the local-path helper image" >&2
    exit 1
  }
  helper_patch=$(jq -cn --arg manifest "${helper_pod_yaml}" \
    '{data: {"helperPod.yaml": $manifest}}')
  ${kubectl} -n local-path-storage patch configmap local-path-config \
    --type=merge -p "${helper_patch}"
  ${kubectl} -n local-path-storage rollout restart deployment/local-path-provisioner
  ${kubectl} -n local-path-storage rollout status \
    deployment/local-path-provisioner --timeout=300s
fi

storage_probe=ai-build-tools-storage-preflight
storage_probe_namespace=${STORAGE_PROBE_NAMESPACE:-default}
${kubectl} -n "${storage_probe_namespace}" delete pod "${storage_probe}" \
  --ignore-not-found --wait=true
${kubectl} -n "${storage_probe_namespace}" delete pvc "${storage_probe}" \
  --ignore-not-found --wait=true
cat <<EOF | ${kubectl} apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${storage_probe}
  namespace: ${storage_probe_namespace}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ${STORAGE_CLASS}
  resources:
    requests:
      storage: 1Mi
---
apiVersion: v1
kind: Pod
metadata:
  name: ${storage_probe}
  namespace: ${storage_probe_namespace}
spec:
  restartPolicy: Never
  containers:
    - name: storage-probe
      image: ${LOCAL_PATH_HELPER_IMAGE}
      command: ["/bin/sh", "-ceu"]
      args:
        - |
          printf 'local-path-ready\n' >/data/probe
          sync
          sleep 600
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ${storage_probe}
EOF
${kubectl} -n "${storage_probe_namespace}" wait --for=condition=Ready \
  "pod/${storage_probe}" --timeout=300s
${kubectl} -n "${storage_probe_namespace}" exec \
  "pod/${storage_probe}" -- cat /data/probe |
  tee "${evidence_dir}/storage-preflight.txt"
${kubectl} -n "${storage_probe_namespace}" get pvc "${storage_probe}" -o yaml \
  >"${evidence_dir}/storage-preflight-pvc.yaml"
probe_pv=$(${kubectl} -n "${storage_probe_namespace}" get pvc "${storage_probe}" \
  -o jsonpath='{.spec.volumeName}')
${kubectl} get pv "${probe_pv}" -o yaml \
  >"${evidence_dir}/storage-preflight-pv.yaml"
${kubectl} -n "${storage_probe_namespace}" delete pod "${storage_probe}" --wait=true
${kubectl} -n "${storage_probe_namespace}" delete pvc "${storage_probe}" --wait=true

cd "${source_dir}"
${kustomize} build common/kubeflow-namespace/base | ${kubectl} apply -f -

${kustomize} build applications/pipeline/upstream/cluster-scoped-resources |
  ${kubectl} apply --server-side --force-conflicts -f -
${kubectl} wait --for=condition=Established crd/applications.app.k8s.io --timeout=120s
${kustomize} build applications/pipeline/upstream/env/platform-agnostic |
  ${kubectl} apply --server-side --force-conflicts -f -

# Envoy otherwise derives one worker per visible CPU. Large GPU hosts can then
# exhaust the container's open-file limit before the metadata proxy starts.
envoy_container=$(${kubectl} -n kubeflow get deployment metadata-envoy-deployment \
  -o jsonpath='{.spec.template.spec.containers[0].name}')
envoy_config=$(${kubectl} -n kubeflow get deployment metadata-envoy-deployment \
  -o jsonpath='{.spec.template.spec.containers[0].args[0]}')
envoy_patch=$(jq -cn \
  --arg name "${envoy_container}" \
  --arg config "${envoy_config}" \
  --arg concurrency "${METADATA_ENVOY_CONCURRENCY}" \
  '{spec: {template: {spec: {containers: [{name: $name, args: [$config, "--concurrency", $concurrency]}]}}}}')
${kubectl} -n kubeflow patch deployment metadata-envoy-deployment \
  --type=strategic -p "${envoy_patch}"
${kubectl} -n kubeflow rollout status deployment/metadata-envoy-deployment --timeout=300s
${kubectl} -n kubeflow get deployment metadata-envoy-deployment -o yaml \
  >"${evidence_dir}/metadata-envoy-deployment.yaml"

${kubectl} wait -n kubeflow --for=condition=Available deployment/mysql --timeout=900s
${kubectl} wait -n kubeflow --for=condition=Available deployment/seaweedfs --timeout=900s
${kubectl} wait -n kubeflow --for=condition=Available deployment/ml-pipeline --timeout=900s
${kubectl} wait -n kubeflow --for=condition=Available deployment/ml-pipeline-ui --timeout=900s

${kustomize} build applications/hub/upstream/overlays/db |
  ${kubectl} -n kubeflow apply --server-side --force-conflicts -f -
${kubectl} wait -n kubeflow --for=condition=Available deployment/model-registry-db --timeout=600s
${kubectl} wait -n kubeflow --for=condition=Available deployment/model-registry-deployment --timeout=600s

${kustomize} build common/cert-manager/base | ${kubectl} apply --server-side --force-conflicts -f -
${kubectl} wait -n cert-manager --for=condition=Ready pod -l app=webhook --timeout=300s
${kustomize} build common/cert-manager/overlays/kubeflow |
  ${kubectl} apply --server-side --force-conflicts -f -
${kubectl} wait -n cert-manager --for=condition=Ready pod \
  -l app.kubernetes.io/instance=cert-manager --timeout=300s

${kustomize} build applications/katib/upstream/installs/katib-cert-manager |
  ${kubectl} apply --server-side --force-conflicts -f -
${kubectl} wait --for=condition=Established crd/experiments.kubeflow.org --timeout=120s
${kubectl} wait --for=condition=Established crd/trials.kubeflow.org --timeout=120s
for deployment in katib-controller katib-db-manager katib-mysql katib-ui; do
  ${kubectl} wait -n kubeflow --for=condition=Available \
    "deployment/${deployment}" --timeout=600s
done

for attempt in 1 2 3; do
  if ${kustomize} build applications/kserve/kserve |
    ${kubectl} apply --server-side --force-conflicts -f -; then
    break
  fi
  [[ ${attempt} -lt 3 ]] || exit 1
  for crd in \
    inferenceservices.serving.kserve.io \
    servingruntimes.serving.kserve.io \
    clusterservingruntimes.serving.kserve.io; do
    ${kubectl} wait --for=condition=Established "crd/${crd}" --timeout=120s
  done
  ${kubectl} wait -n kubeflow --for=condition=Available \
    deployment/kserve-controller-manager --timeout=600s
  sleep 5
done
${kubectl} wait --for=condition=Established crd/inferenceservices.serving.kserve.io --timeout=120s
${kubectl} wait -n kubeflow --for=condition=Available deployment/kserve-controller-manager --timeout=600s

${kubectl} apply -f "${root_dir}/kubernetes/manifests/workflow-integration.yaml"
access_key=$(${kubectl} -n kubeflow get secret mlpipeline-minio-artifact -o jsonpath='{.data.accesskey}' | base64 -d)
secret_key=$(${kubectl} -n kubeflow get secret mlpipeline-minio-artifact -o jsonpath='{.data.secretkey}' | base64 -d)
${kubectl} -n "${WORKLOAD_NAMESPACE}" create secret generic ai-build-tools-s3 \
  --from-literal=AWS_ACCESS_KEY_ID="${access_key}" \
  --from-literal=AWS_SECRET_ACCESS_KEY="${secret_key}" \
  --dry-run=client -o yaml |
  ${kubectl} apply -f -
${kubectl} -n "${WORKLOAD_NAMESPACE}" annotate secret ai-build-tools-s3 \
  serving.kserve.io/s3-endpoint="${S3_ENDPOINT}" \
  serving.kserve.io/s3-usehttps="${S3_USE_HTTPS}" \
  serving.kserve.io/s3-region=us-east-1 \
  serving.kserve.io/s3-useanoncredential=false --overwrite

KUBECTL_BIN="${kubectl}" "${root_dir}/kubernetes/platform/verify.sh" |
  tee "${evidence_dir}/platform-verification.txt"
