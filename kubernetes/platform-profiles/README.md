# Composable AI Platform Profiles

This directory builds immutable Helm wrapper charts for mCAPI add-on profiles.
The wrappers vendor rendered upstream manifests, separate CRDs into Helm's
pre-install phase, and replace every runtime image tag with a locked manifest
digest.

The profile set is intentionally split by lifecycle boundary:

| Chart | Responsibility |
|---|---|
| `ai-foundation` | cert-manager and shared Kubeflow namespaces |
| `ai-platform-core` | cert-manager-dependent issuer, Kubeflow Pipelines, and Model Registry |
| `ai-scheduling-kueue` | Kueue admission controller |
| `ai-training-h200` | JobSet and Kubeflow Trainer v2 controllers |
| `ai-serving-h200` | KServe control plane |
| `ai-workflow-bootstrap` | Torch runtime, seven-GPU training queue, one-GPU serving reserve, workflow RBAC, and platform defaults |

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

The packaged KFP deployment is single-user, so its API creates Argo Workflows
and task Pods in `kubeflow`. The workflow bootstrap therefore defaults its
ServiceAccount and LocalQueue to `kubeflow`, and the selected workspace profile
must create the workflow PVC there as well. A deployment that overrides this
namespace must move all three resources together; Kubernetes does not permit a
Pod to mount a PVC from another namespace.

Publication is performed by
`.github/workflows/publish-ai-platform-charts.yml`. The workflow uses the
repository `GITHUB_TOKEN` with package-write permission and publishes to
`oci://ghcr.io/ricolin/ai-k8s-charts`.

The charts install controllers and cluster-birth defaults only. Datasets,
training configurations, model releases, inference requests, and run evidence
remain workload resources.

The default eight-H200 contract exposes all eight devices for readiness, but
admits at most seven training GPUs through `h200-ai`. The remaining device is
an explicit KServe reserve. KFP tasks themselves remain CPU-only and create
Kueue-managed `TrainJob` or batch `Job` resources, so training no longer
requires deleting a live one-GPU predictor.

The webhook-validated `ClusterTrainingRuntime/torch-distributed` is owned by
`ai-workflow-bootstrap`, whose add-on profile depends on a Ready
`ai-training-h200` release. Do not move it back into the Trainer controller
release: creating the runtime while cert-manager is still rotating the Trainer
webhook certificate can fail with an unknown-authority error.

The generated controller workloads tolerate both current and legacy
control-plane taints. This permits an explicitly selected all-in-one AI control
plane without removing the Kubernetes taint and is inert on worker-backed
clusters. The cert-manager-dependent `ClusterIssuer` is intentionally owned by
`ai-platform-core`, whose profile depends on a ready `ai-foundation`; keeping
the issuer in the cert-manager release creates a webhook bootstrap race.
