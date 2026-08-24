# Local-Path Storage Add-On

This opt-in Helm chart packages the reviewed local-path provisioner for
single-node and edge-style AI workflow clusters. It keeps the provisioner and
helper images digest-pinned, creates the StorageClass only when enabled, and
defaults to `WaitForFirstConsumer` so a PVC is bound on the workload node that
will consume it.

The provisioner and helper Pods use the upstream
`local-path-provisioner-service-account` identity. Keep the ServiceAccount,
RoleBinding subject, and Deployment field synchronized; the provisioner copies
that identity onto helper Pods during volume creation.

The chart is a storage implementation, not the retained model-workspace
contract. Compose it before `ai-model-workspace`; the latter creates the
namespace, quota, retained PVC, and write/read acceptance Job.

```bash
./kubernetes/addons/local-path-storage/test-render.sh
helm template local-path-storage \
  kubernetes/addons/local-path-storage \
  --set enabled=true
```

`nodePath` is a deployment policy input. Do not put site addresses, node names,
or credentials in this public chart. Production deployments that require
replicated or multi-node storage should publish a different storage profile
instead of presenting local-path as shared storage.

The promoted source commit, successful publication workflow, package checksum,
and registry-returned OCI digest are recorded in `artifacts.lock.yaml`.
