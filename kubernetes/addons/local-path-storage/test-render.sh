#!/usr/bin/env bash
set -euo pipefail
chart_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
temporary=$(mktemp -d /tmp/local-path-storage-render.XXXXXX)
trap 'rm -rf "${temporary}"' EXIT

helm lint "${chart_dir}" >/dev/null
helm template inactive "${chart_dir}" >"${temporary}/inactive.yaml"
! grep -q '^kind:' "${temporary}/inactive.yaml"

helm template local-path "${chart_dir}" --set enabled=true \
  >"${temporary}/active.yaml"
for kind in Namespace ServiceAccount Role ClusterRole RoleBinding \
  ClusterRoleBinding ConfigMap Deployment StorageClass; do
  grep -Fq "kind: ${kind}" "${temporary}/active.yaml"
done
grep -Fq 'pod-security.kubernetes.io/enforce: privileged' \
  "${temporary}/active.yaml"
grep -Fq 'docker.io/rancher/local-path-provisioner@sha256:' \
  "${temporary}/active.yaml"
grep -Fq 'docker.io/library/busybox@sha256:' "${temporary}/active.yaml"
grep -Fq 'storageclass.kubernetes.io/is-default-class: "true"' \
  "${temporary}/active.yaml"

if helm template invalid "${chart_dir}" --set enabled=true \
  --set provisioner.image=rancher/local-path-provisioner:latest \
  >"${temporary}/invalid.yaml" 2>"${temporary}/invalid.err"; then
  echo "mutable provisioner image unexpectedly rendered" >&2
  exit 1
fi
grep -Fq 'provisioner.image must be pinned by sha256 digest' \
  "${temporary}/invalid.err"
echo "PASS: local-path storage render contract"
