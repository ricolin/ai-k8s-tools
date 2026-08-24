# Composable AI Platform Profiles

This directory builds immutable Helm wrapper charts for mCAPI add-on profiles.
The wrappers vendor rendered upstream manifests, separate CRDs into Helm's
pre-install phase, and replace every runtime image tag with a locked manifest
digest.

The profile set is intentionally split by lifecycle boundary:

| Chart | Responsibility |
|---|---|
| `ai-foundation` | cert-manager and shared Kubeflow namespaces |
| `ai-platform-core` | Kubeflow Pipelines and Model Registry |
| `ai-scheduling-kueue` | Kueue admission controller |
| `ai-training-h200` | Kubeflow Trainer v2 and Torch runtime |
| `ai-serving-h200` | KServe control plane |
| `ai-workflow-bootstrap` | H200 queue, workflow RBAC, and platform defaults |

`build.sh` reconstructs generated resources from `source-lock.env` and
`image-lock.tsv`. It requires `git`, `curl`, `helm`, Python 3 with PyYAML, and
network access. `test.sh` is offline after the generated resources exist.

```bash
./kubernetes/platform-profiles/build.sh
./kubernetes/platform-profiles/test.sh
```

After a workload cluster is born from the complete profile set, verify the
workload-side contract without reinstalling or mutating the platform:

```bash
export KUBECONFIG=/path/to/workload-kubeconfig
EXPECTED_GPU_COUNT=8 \
  ./kubernetes/platform-profiles/verify.sh
```

The verifier follows the packaged ownership split: KFP and KServe run in
`kubeflow`, Model Registry runs in `default`, Kueue runs in `kueue-system`,
and Kubeflow Trainer runs in `kubeflow-system`. Katib is not required because
it remains an optional future profile.

Publication is performed by
`.github/workflows/publish-ai-platform-charts.yml`. The workflow uses the
repository `GITHUB_TOKEN` with package-write permission and publishes to
`oci://ghcr.io/ricolin/ai-k8s-charts`.

The charts install controllers and cluster-birth defaults only. Datasets,
training configurations, model releases, inference requests, and run evidence
remain workload resources.
