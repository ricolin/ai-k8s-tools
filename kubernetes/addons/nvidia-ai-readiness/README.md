# NVIDIA AI Readiness Add-On

This opt-in wrapper installs NVIDIA GPU Operator `v26.7.0` and makes CAAPH
release readiness depend on ordered one-GPU and full-node CUDA checks. The
chart renders no Kubernetes objects with its defaults. Existing mCAPI
templates, Ubuntu 22.04 images, other Kubernetes tags, and CPU clusters are
unchanged unless an operator explicitly selects an add-on profile that enables
this chart.

The first profile uses Operator-managed drivers and Container Toolkit. The
preinstalled-runtime fallback is present as a disabled contract only; enabling
it requires a separately accepted guest image and explicit values.

## Render

Build the locked dependency and provide content-addressed readiness images:

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm dependency build kubernetes/addons/nvidia-ai-readiness
helm template nvidia-ai-h200 \
  kubernetes/addons/nvidia-ai-readiness \
  --namespace gpu-operator \
  -f kubernetes/addons/nvidia-ai-readiness/profiles/managed-driver-values.yaml \
  --set readiness.expectedGpuNodes=1 \
  --set readiness.expectedGpuCountPerNode=8 \
  --set readiness.fullNodeGpuCount=8 \
  --set readiness.orchestratorImage=REGISTRY/readiness@sha256:DIGEST \
  --set readiness.cudaSmokeImage=REGISTRY/cuda-smoke@sha256:DIGEST
```

`Chart.lock` pins the dependency metadata and the packaged upstream chart
archive SHA-256 is recorded in the release identity ConfigMap. GPU Operator
`v26.7.0` itself expresses operand images as repository/image/version rather
than OCI digest references. Promotion therefore also requires immutable
mirror tags plus observed runtime `imageID` digests in the readiness evidence;
the wrapper does not falsely describe those upstream fields as digest pins.

The orchestrator can create Jobs and update its evidence ConfigMap only in the
release namespace. Its ClusterRole is read-only and limited to Nodes and the
NVIDIA ClusterPolicy; device-plugin access is namespace scoped. All stages
have explicit deadlines and the child Jobs consume no GPU after completion.
The generated one-GPU and full-node CUDA Jobs inherit the readiness Pod's node
selector, tolerations, image pull secrets, and pull policy. This is required
when a reviewed GPU node is also a tainted Kubernetes control-plane node.
Evidence includes the requested content-addressed images, the readiness Pod's
observed runtime `imageID`, GPU Operator Pod runtime `imageID` values, node GPU
capacity, ClusterPolicy state, stable GPU UUIDs, and both CUDA test logs. The
retained objects are projected to audit-relevant fields before the ConfigMap is
created or replaced; the payload is not duplicated into a last-applied
annotation.

The profile is not inferred from an operating-system name or Kubernetes tag.
It is disabled by default, checks the configured minimum Kubernetes minor plus
Linux/amd64 requirements at runtime, and keeps the preinstalled-driver fallback
disabled. This lets Ubuntu 22.04 and other existing images continue through the
unchanged mCAPI path unless an operator deliberately opts them in.

Run the offline contract:

```bash
./kubernetes/addons/nvidia-ai-readiness/test-render.sh
```

`images/source-lock.env` pins the Ubuntu, CUDA devel/runtime, and kubectl build
inputs. The manually dispatched `NVIDIA AI readiness` workflow always builds
the amd64 images and chart; it publishes to the named GHCR repositories only
when its `publish` input is explicitly true. Promotion must resolve the
registry-returned image and chart digests and consume those digests rather than
the `0.1.1` transport tags.

The accepted public artifacts, source commit, and successful publication run
are recorded in `artifacts.lock.yaml`. Anonymous registry requests must return
HTTP 200 for every locked reference before a deployment consumes them.
