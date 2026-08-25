#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: render-manifest-in-cluster.sh \
  --namespace NAMESPACE --pod POD --image IMAGE --image-id SHA256 \
  --node-selector-key KEY --node-selector-value VALUE --output FILE \
  [--pvc NAME --pvc-mount PATH] [--tolerate-control-plane] -- COMMAND [ARG ...]

COMMAND must accept an --output option and write one JSON manifest to it.
The renderer Pod, dry-run output, and live Pod are retained beside FILE.
EOF
}

namespace=
pod=
image=
image_id=
node_selector_key=
node_selector_value=
output=
pvc=
pvc_mount=/workspace
tolerate_control_plane=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --namespace) namespace=${2-}; shift 2 ;;
    --pod) pod=${2-}; shift 2 ;;
    --image) image=${2-}; shift 2 ;;
    --image-id) image_id=${2-}; shift 2 ;;
    --node-selector-key) node_selector_key=${2-}; shift 2 ;;
    --node-selector-value) node_selector_value=${2-}; shift 2 ;;
    --output) output=${2-}; shift 2 ;;
    --pvc) pvc=${2-}; shift 2 ;;
    --pvc-mount) pvc_mount=${2-}; shift 2 ;;
    --tolerate-control-plane) tolerate_control_plane=true; shift ;;
    --) shift; break ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

for value in namespace pod image image_id node_selector_key node_selector_value output; do
  [[ -n ${!value} ]] || { echo "missing --${value//_/-}" >&2; exit 2; }
done
[[ $# -gt 0 ]] || { echo "renderer command is required" >&2; exit 2; }
[[ ${namespace} =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || { echo "invalid namespace" >&2; exit 2; }
[[ ${pod} =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#pod} -le 63 ]] || {
  echo "invalid renderer Pod name" >&2
  exit 2
}
[[ ${image} == *:* && ${image} != *@* ]] || {
  echo "renderer image must be a node-local tag" >&2
  exit 2
}
[[ ${image_id} =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "invalid image ID" >&2; exit 2; }
[[ ${pvc_mount} == /* ]] || { echo "PVC mount must be absolute" >&2; exit 2; }
if [[ -n ${pvc} ]]; then
  [[ ${pvc} =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || { echo "invalid PVC name" >&2; exit 2; }
fi

kubectl_bin=${KUBECTL_BIN:-kubectl}
manifest=${output}.renderer-pod.json
dry_run=${output}.renderer-dry-run.txt
live=${output}.renderer-pod.live.yaml
renderer_log=${output}.renderer.log
temporary=${output}.new
mkdir -p "$(dirname "${output}")"
for path in \
  "${output}" "${manifest}" "${dry_run}" "${live}" \
  "${renderer_log}" "${temporary}"; do
  [[ ! -e ${path} ]] || { echo "refusing to overwrite evidence: ${path}" >&2; exit 1; }
done

python3 - "${manifest}" "${namespace}" "${pod}" "${image}" "${image_id}" \
  "${node_selector_key}" "${node_selector_value}" "${pvc}" "${pvc_mount}" \
  "${tolerate_control_plane}" "$@" <<'PY'
import json
import sys

(
    destination,
    namespace,
    pod_name,
    image,
    image_id,
    selector_key,
    selector_value,
    pvc,
    pvc_mount,
    tolerate_control_plane,
    *command,
) = sys.argv[1:]

pod_spec = {
    "restartPolicy": "Never",
    "automountServiceAccountToken": False,
    "nodeSelector": {selector_key: selector_value},
    "securityContext": {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "fsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    },
    "containers": [
        {
            "name": "renderer",
            "image": image,
            "imagePullPolicy": "Never",
            "command": [
                "/bin/sh",
                "-c",
                '"$@" --output /tmp/rendered.json && cat /tmp/rendered.json',
                "renderer",
                *command,
            ],
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "2", "memory": "2Gi"},
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "readOnlyRootFilesystem": True,
            },
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        }
    ],
    "volumes": [{"name": "tmp", "emptyDir": {}}],
}
if tolerate_control_plane == "true":
    pod_spec["tolerations"] = [
        {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
        {
            "key": "node-role.kubernetes.io/master",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
    ]
if pvc:
    pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "workspace", "mountPath": pvc_mount, "readOnly": True}
    )
    pod_spec["volumes"].append(
        {"name": "workspace", "persistentVolumeClaim": {"claimName": pvc}}
    )

document = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": pod_name,
        "namespace": namespace,
        "annotations": {"ai-k8s-tools.ricolin.dev/node-local-image-id": image_id},
    },
    "spec": pod_spec,
}
with open(destination, "w", encoding="utf-8") as stream:
    json.dump(document, stream, indent=2)
    stream.write("\n")
PY

if "${kubectl_bin}" -n "${namespace}" get pod "${pod}" >/dev/null 2>&1; then
  echo "refusing to reuse renderer Pod: ${namespace}/${pod}" >&2
  exit 1
fi
"${kubectl_bin}" apply --dry-run=server -f "${manifest}" >"${dry_run}"
"${kubectl_bin}" create -f "${manifest}"
phase=
for _ in $(seq 1 150); do
  phase=$("${kubectl_bin}" -n "${namespace}" get pod "${pod}" \
    -o jsonpath='{.status.phase}')
  [[ ${phase} == Succeeded || ${phase} == Failed ]] && break
  sleep 2
done
"${kubectl_bin}" -n "${namespace}" get pod "${pod}" -o yaml >"${live}"
"${kubectl_bin}" -n "${namespace}" logs "${pod}" >"${renderer_log}" 2>&1 || true
if [[ ${phase} != Succeeded ]]; then
  echo "renderer Pod did not succeed: ${namespace}/${pod}: ${phase:-timeout}" >&2
  exit 1
fi
cp "${renderer_log}" "${temporary}"
python3 -m json.tool "${temporary}" >/dev/null
mv -f "${temporary}" "${output}"
printf 'rendered=%s\nrenderer_pod=%s/%s\n' "${output}" "${namespace}" "${pod}"
