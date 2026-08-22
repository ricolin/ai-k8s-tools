# Kubeflow And KServe Bundle

This versioned workload bundle installs the repository's pinned Kubeflow
Pipelines, Model Registry, Katib, cert-manager, and KServe subset after an
mCAPI cluster is infrastructure-ready. It is deliberately independent of the
NVIDIA `addon_profile`: application lifecycle does not block Magnum cluster
creation or deletion.

```bash
export KUBECONFIG=/path/to/workload/config
export PROFILE_FILE=/path/to/reviewed-platform-profile.env
export EVIDENCE_DIR=/path/to/retained/evidence/platform
./kubernetes/bundles/kubeflow-kserve/install.sh
```

The installer runs preflight, uses the exact commit in `source-lock.env`, and
records the source identities and readiness output. Uninstall is a separate,
explicit data-owner operation. It refuses to proceed while PVCs exist unless
`ALLOW_KUBEFLOW_DATA_DELETE=true` is deliberately supplied.

```bash
./kubernetes/bundles/kubeflow-kserve/test-contract.sh
./kubernetes/bundles/kubeflow-kserve/verify.sh
```
