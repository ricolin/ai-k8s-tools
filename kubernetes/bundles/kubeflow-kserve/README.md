# Kubeflow And KServe Bundle

This versioned workload bundle installs the repository's pinned Kubeflow
Pipelines, Model Registry, Katib, cert-manager, and KServe subset after an
mCAPI cluster is infrastructure-ready. It is deliberately independent of the
NVIDIA profile. In the existing imperative path, application lifecycle does
not block Magnum cluster creation or deletion.

This directory is not yet a CAAPH profile. A born-ready mCAPI product must
package the owned components into immutable Helm-compatible wrappers, publish
separate lifecycle profiles with explicit dependencies, and select those
profiles through the template-owned plural `addon_profiles` contract. Until
that publication gate passes, this installer remains the accepted fallback
path and must not be described as profile-managed cluster readiness.

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
