# AI Model Workspace Bundle

This opt-in Helm chart creates the workflow namespace, storage quota, retained
PVC, and a bounded write/read validation Job. It supports an existing dynamic
StorageClass or an explicitly configured local static PV with node affinity.

The default renders nothing. Static-local mode requires every site-specific
path, object name, and node identity as values. `retentionPolicy=Keep` adds
Helm keep annotations and the PV uses `Retain`; deletion therefore requires an
explicit data-owner decision rather than silently removing model artifacts.

```bash
./kubernetes/bundles/workspace/test-render.sh
helm upgrade --install ai-model-workspace \
  kubernetes/bundles/workspace \
  -f reviewed-workspace-values.yaml
kubectl -n ai-workflows wait --for=condition=Complete \
  job/ai-model-workspace-validation --timeout=5m
```

The release also records the chart version, storage mode, retention policy,
and PVC name in `<release>-identity`. Use that ConfigMap together with the OCI
manifest digest when collecting profile evidence.

## Publish

The `AI platform bundles` workflow validates the workspace and pinned platform
contracts on every manual run. Set its `publish` input to `true` only for a
reviewed source commit. It publishes:

```text
oci://ghcr.io/ricolin/ai-k8s-charts/ai-model-workspace:<chart-version>
```

After publication, resolve and record the registry-returned manifest digest.
CAAPH selects the immutable chart version, while deployment preflight must
verify that the version still resolves to the reviewed digest before a cluster
template selects the profile.

The currently promoted source, workflow, chart package checksum, and OCI
manifest digest are recorded in `artifacts.lock.yaml`. Treat the file as an
append-by-new-version publication record: never overwrite a published tag with
different chart bytes.
