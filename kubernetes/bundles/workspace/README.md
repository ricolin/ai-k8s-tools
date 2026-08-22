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
