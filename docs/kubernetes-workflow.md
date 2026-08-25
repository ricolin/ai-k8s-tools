# Kubernetes model workflow with Kubeflow and KServe

## Purpose

`ai-k8s-tools` runs an immutable Kubernetes model lifecycle
using established Kubernetes AI components:

- Kubeflow Pipelines (KFP) orchestrates visible, retryable workflow steps.
- S3-compatible KFP artifacts carry model inputs, adapters, generated
  acceptance outputs, evaluation reports, and release evidence.
- Kubeflow Hub records model versions, lineage, artifact references, and
  candidate/released lifecycle state.
- KServe loads the base and adapter as separate read-only artifacts and
  exposes a fresh inference process for release verification.

The reference workflow is:

```text
train candidate A
  -> generate acceptance images
  -> evaluate
  -> register A as CANDIDATE
  -> deploy base + A through KServe
  -> verify inference from a separate process
  -> promote A to RELEASED
  -> train B using released A as its parent
  -> generate, evaluate, deploy, and verify B
  -> promote B to RELEASED
```

This is derived training, not an in-run optimizer-checkpoint resume. The
second training run starts from an accepted parent release and records that
release in its lineage.

## Evidence boundary

The included fixture implementation creates deterministic artifacts with the
same interfaces used by the Kubernetes workflow. It validates:

- KFP orchestration and artifact transfer;
- candidate and release transitions;
- Hub registration and A-to-B lineage;
- KServe base-plus-adapter storage initialization;
- fresh-process inference; and
- promotion only after evaluation and inference pass.

Fixture output is always mechanics evidence. It does not prove CUDA behavior,
distributed training, physical-GPU performance, real LoRA quality, or model
fitness. A physical implementation must replace the fixture training and
serving images and produce its own evidence.

## Repository layout

```text
kubernetes/
├── versions.env
├── profiles/
│   ├── kubernetes-fixture.env # provider-neutral mechanics profile
│   ├── mcapi-emulated.env     # mCAPI mechanics example
│   └── nvidia-h200.env        # physical NVIDIA configuration contract
├── platform/
│   ├── install.sh
│   └── verify.sh
├── workflows/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── Dockerfile
│   ├── src/ai_build_tools_k8s/
│   │   ├── pipeline.py
│   │   ├── workflow.py
│   │   ├── orchestrate.py
│   │   └── server.py
│   └── tests/
├── serving/
│   └── Dockerfile
├── manifests/
│   └── workflow-integration.yaml
├── tools/
│   ├── build-images.sh
│   └── run-workflow.sh
└── mcapi/
    ├── enable-node-egress.sh
    └── publish-images.sh
```

## Pinned platform baseline

`kubernetes/versions.env` records the validated platform set:

- Kubeflow Community Distribution `26.03.1`
- source commit `f09f3eeaa25cc852665f460497a42b7fc68639ac`
- Kubeflow Pipelines `2.16.1`
- Kubeflow Hub server `0.3.9`
- KServe `0.18.0`
- cert-manager `1.20.2`
- local-path-provisioner `v0.0.37` for disposable environments
- BusyBox `1.36.1` for the pinned local-path helper and storage preflight
- Kustomize `v5.8.1`
- uv `0.10.9`

Treat the distribution commit as authoritative and review version changes as
one platform update.

## Local development

```bash
uv sync --project kubernetes/workflows --python 3.12 --frozen
uv run --project kubernetes/workflows --frozen \
  pytest -q kubernetes/workflows/tests

bash -n kubernetes/tools/*.sh kubernetes/mcapi/*.sh \
  kubernetes/platform/*.sh
git diff --check
```

Compile both KFP packages with a digest-pinned workflow image:

```bash
output_dir=$(mktemp -d /tmp/ai-build-tools-compiled.XXXXXX)

uv run --project kubernetes/workflows --frozen python \
  -m ai_build_tools_k8s.pipeline \
  --workflow-image registry.example.com/ai-build-tools@sha256:<digest> \
  --node-selector-key node.cluster.x-k8s.io/nodegroup \
  --node-selector-value gpu-workers \
  --s3-endpoint-url http://seaweedfs.kubeflow.svc.cluster.local:9000 \
  --output-dir "${output_dir}"
```

This emits:

- `sdxl-lora-train-and-register.yaml`
- `sdxl-lora-deploy-verify-release.yaml`

## Install on a Kubernetes cluster

Set a kubeconfig for the target cluster and select a profile:

```bash
export KUBECONFIG=/path/to/target-kubeconfig
export PROFILE_FILE=$PWD/kubernetes/profiles/kubernetes-fixture.env

EVIDENCE_DIR=$PWD/evidence/platform \
  ./kubernetes/platform/install.sh 2>&1 | \
  tee evidence/platform-install.log

./kubernetes/platform/verify.sh | \
  tee evidence/platform/platform-verification.txt
```

The installer verifies the pinned source commit before rendering it, installs
the minimal KFP, Hub, cert-manager, KServe, and storage set, and waits for the
required deployments. Before applying Kubeflow, it pins the local-path helper
image and proves dynamic provisioning with a temporary PVC and mounted probe
Pod. It also caps the KFP metadata Envoy at four workers so hosts with a large
CPU count do not exhaust the container open-file limit. KServe retries wait on
its own CRDs and controller instead of unrelated namespace Pods.

The included local-path StorageClass is suitable for disposable validation
only. Production deployments must select durable storage and backup policies.
Successful installation records the storage probe and rendered metadata Envoy
deployment under `EVIDENCE_DIR`.

## mCAPI mechanics example

The mCAPI profile is an integration example, not a hard-coded environment. It
expects the worker node group label:

```text
node.cluster.x-k8s.io/nodegroup=gpu-workers
```

Copy `kubernetes/profiles/mcapi-emulated.env` or override its values through
environment variables. The repository intentionally contains no target host,
kubeconfig, SSH key, registry hostname, VLAN, or gateway identity.

### Optional emulated-node egress

Some nested all-in-one deployments require the management host to provide
temporary image-pull egress to emulated nodes. When that applies, set the
site-owned values explicitly:

```bash
export KUBECONFIG=/path/to/target-kubeconfig
export MCAPI_NODE_CIDR=192.0.2.0/24
export MCAPI_NODE_GATEWAY=192.0.2.10
export MCAPI_EXTERNAL_INTERFACE=eth0
export MCAPI_NODE_SSH_USER=ubuntu
export MCAPI_NODE_SSH_KEY=/path/to/node-key

EVIDENCE_DIR=$PWD/evidence/mcapi-egress \
  ./kubernetes/mcapi/enable-node-egress.sh
```

The helper installs an idempotent, source-CIDR-scoped masquerade rule,
discovers node InternalIP addresses through the selected kubeconfig, records
routes before and after the change, and verifies registry reachability. Guest
routes are runtime state and must be restored after node reboot.

Do not run this helper when the cluster already has supported egress.

### Build images

Use a new immutable revision for every source change:

```bash
revision=kfp-$(date -u +%Y%m%dt%H%M%Sz)
mkdir -p "evidence/build-${revision}"

./kubernetes/tools/build-images.sh "${revision}" 2>&1 | \
  tee "evidence/build-${revision}/build.log"
```

### Publish images

`publish-images.sh` is an optional helper for a registry running in a
management Kubernetes cluster with the repository's read-only maintenance
convention. It requires all environment identities explicitly:

```bash
export HOST_KUBECONFIG=/path/to/registry-cluster-kubeconfig
export REGISTRY_HOST=registry.example.com
export REGISTRY_NAMESPACE=ai-release-registry

# Optional site-owned route or ingress manifest:
export REGISTRY_ROUTE_MANIFEST=/path/to/registry-route.yaml

mkdir -p "evidence/publish-${revision}"
./kubernetes/mcapi/publish-images.sh "${revision}" \
  "evidence/publish-${revision}" 2>&1 | \
  tee "evidence/publish-${revision}/publish.log"
```

The helper:

- refuses to overwrite an existing tag;
- temporarily removes the read-only setting;
- publishes through a local port-forward;
- records the resulting immutable digests;
- restores the original ingress resources and read-only setting in its exit
  trap; and
- verifies external digest reads and rejected writes.

For any other registry, publish the two images with the registry's supported
workflow and create an `images.env` file containing:

```bash
AI_BUILD_TOOLS_WORKFLOW_IMAGE=registry.example.com/ai-build-tools@sha256:<digest>
AI_BUILD_TOOLS_RUNTIME_IMAGE=registry.example.com/ai-build-tools@sha256:<digest>
```

### Run and release A and B

```bash
export KUBECONFIG=/path/to/target-kubeconfig
export PROFILE_FILE=$PWD/kubernetes/profiles/mcapi-emulated.env

run_label=mcapi-$(date -u +%m%dt%H%M%S)
mkdir -p "evidence/run-${run_label}"

./kubernetes/tools/run-workflow.sh "${run_label}" \
  "evidence/publish-${revision}/images.env" \
  "evidence/run-${run_label}" 2>&1 | \
  tee "evidence/run-${run_label}/run.log"
```

Every retry must use a new run label. Failed KFP runs remain queryable and
existing Hub versions are never overwritten.

## Verification and use

### Overall result

```bash
jq . "evidence/run-${run_label}/workflow-result.json"
jq -e '.status == "PASS" and .evidence_level == "mechanics"' \
  "evidence/run-${run_label}/workflow-result.json"
```

The result records all four KFP run IDs, Hub artifact and version IDs,
immutable artifact URIs, lifecycle states, and A-to-B parent lineage.

### KFP placement

```bash
kubectl -n kubeflow get workflows.argoproj.io \
  --sort-by=.metadata.creationTimestamp

kubectl -n kubeflow get pods -o wide \
  -l pipelines.kubeflow.org/v2_component=true
```

### Hub releases and lineage

```bash
kubectl -n kubeflow port-forward service/model-registry-service 8081:8080
curl -fsS \
  http://127.0.0.1:8081/api/model_registry/v1alpha3/registered_models | jq
```

Verify that:

- A and B are separate immutable model versions;
- both have `lifecycle_status=RELEASED`;
- B has `parent_model_version` equal to A;
- artifact URIs use portable `s3://` references;
- base revision, dataset digest, pipeline run ID, evidence class, adapter
  digest, and KServe service are recorded.

### KServe inference

```bash
kubectl -n kubeflow get inferenceservice,pod,service

service=cute-bear-b-${run_label}
kubectl -n kubeflow port-forward \
  "service/${service}-predictor" 8082:80

curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  -d '{"instances":[{"prompt":"a cute little bear","seed":26081002}]}' \
  "http://127.0.0.1:8082/v1/models/${service}:predict"
```

The generated `InferenceService` uses Standard mode and separate read-only
base and adapter storage URIs. The release pipeline performs inference from a
separate workflow step and promotes the Hub version only after response and
PNG validation pass.

## Physical NVIDIA implementation

`kubernetes/profiles/nvidia-h200.env` is a configuration contract for a
physical implementation. Before making a physical release claim:

1. replace the fixture training component with a digest-pinned CUDA/Diffusers
   image;
2. request the intended `nvidia.com/gpu` count on one node;
3. use durable object and CSI storage;
4. stage an immutable base-model snapshot and dataset manifest;
5. retain the pilot-before-full-training gate;
6. replace the fixture server with a runtime that loads the frozen base and
   LoRA adapter separately; and
7. record driver, CUDA, GPU topology, telemetry, image digests, artifact
   digests, and KFP run IDs as physical evidence.

A mechanics result must never be promoted or relabeled as a physical result.
