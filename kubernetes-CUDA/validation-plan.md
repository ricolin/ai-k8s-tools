# NVIDIA H200 Kubernetes validation plan

## 1. Goal and boundary

The goal is to replace the deterministic CUDA-free implementations in
`ai-k8s-tools/kubernetes` with digest-pinned CUDA implementations and prove
this complete lifecycle on one physical Kubernetes worker with eight NVIDIA
H200 GPUs:

1. train SDXL LoRA candidate A;
2. generate and evaluate with A;
3. promote A as an immutable release;
4. start a distinct training run from released A and produce candidate B;
5. generate and evaluate with B;
6. deploy the base model plus B through KServe; and
7. prove that a fresh serving process loaded B and performed GPU inference.

Single-node, multi-GPU DDP is in scope. Multi-node training, InfiniBand/RoCE
qualification, GPU sharing, MIG, autoscaling, and interrupted-run optimizer
checkpoint resume are not required for the first proof.

Cluster creation is outside this repository. The cluster may be created by
mCAPI or another Kubernetes provider. This runbook consumes a kubeconfig, a
GPU-node selector, the standard `nvidia.com/gpu` resource, durable artifact
storage, Kubeflow Pipelines, Kubeflow Hub, and KServe.

## 2. Work that must be implemented

Do not integrate this path into the normal `kubernetes/` workflow until these
changes are implemented and unit tested:

| Area | Required change |
| --- | --- |
| Profiles | Replace the single `GPU_COUNT` with pilot, training, generation, evaluation, and serving counts. A requested evidence level must not become an observed evidence level. |
| KFP compiler | Give only CUDA tasks a node selector, GPU limit, and optional tolerations. CPU-only resolution, registration, and evidence tasks must remain schedulable on CPU nodes. |
| Input resolver | Materialize the exact base-model revision and dataset into read-only inputs, verify their digests, and keep credentials out of evidence. |
| Training image | Build a digest-pinned CUDA/PyTorch/Diffusers image. It must run the existing SDXL LoRA pilot and full trainer without downloading mutable code at run time. |
| Training component | Replace `train-fixture`; explicitly launch one process per allocated GPU for the eight-GPU run and record rank/world-size/device mapping. |
| Derived training | Materialize release A, verify its digest, load its LoRA weights into the training pipeline, then train new LoRA output B. Do not silently treat A as an optimizer checkpoint. |
| Generation/evaluation | Replace fixture PNG generation with CUDA SDXL inference. Record prompt, seed, base digest, adapter digest, CUDA/PyTorch versions, GPU UUID, output digest, and policy verdict. |
| KServe runtime | Use a CUDA image that loads the immutable base and adapter separately, requests one GPU, becomes ready only after both digests are verified, and reports loaded identities. |
| Evidence | Calculate proof from observed pod specs, node capacity, process/rank records, and output digests. A profile alone is never evidence. |
| Tests | Cover resource assignment, selector placement, derived-parent loading, digest mismatch rejection, DDP world-size mismatch, serving GPU limits, and evidence downgrade/failure behavior. |

Recommended implementation order:

1. CUDA evidence guardrails and resource schema;
2. KFP accelerator assignment and compile-time tests;
3. immutable base-model and dataset staging;
4. one-GPU pilot and generation components;
5. eight-GPU single-node DDP training;
6. released-A-to-derived-B loading;
7. CUDA KServe runtime and fresh-process verification; and
8. end-to-end physical validation and documentation of observed identities.

## 3. Create a run record

Use a unique directory and never reuse it for a retry:

```bash
export REPO_ROOT=$PWD
export H200_PROFILE=/path/to/site-owned-h200.env

set -a
. "${H200_PROFILE}"
set +a

run_id=h200-$(date -u +%Y%m%dT%H%M%SZ)
export EVIDENCE_DIR="${REPO_ROOT}/evidence/${run_id}"
mkdir -p "${EVIDENCE_DIR}"/{cluster,smoke,images,pipeline,serving,release}

kubectl config current-context | tee "${EVIDENCE_DIR}/cluster/context.txt"
kubectl version -o yaml > "${EVIDENCE_DIR}/cluster/kubernetes-version.yaml"
```

Copy the resolved, secret-free configuration into the evidence directory.
Never copy kubeconfigs, bearer tokens, registry credentials, S3 credentials,
or gated-repository tokens into evidence.

## 4. Gate G0: immutable inputs and images

Before allocating a GPU, resolve every image tag to a digest and verify that
the base-model revision and dataset checksum are immutable:

```bash
for name in \
  CUDA_SMOKE_IMAGE PYTORCH_CUDA_IMAGE CUDA_WORKFLOW_IMAGE \
  CUDA_TRAIN_IMAGE CUDA_SERVING_IMAGE; do
  value=$(printenv "${name}")
  case "${value}" in
    *@sha256:*) ;;
    *) echo "${name} is not digest-pinned: ${value}" >&2; exit 1 ;;
  esac
done

test -n "${BASE_MODEL_REVISION}"
test -n "${DATASET_SHA256}"
```

Record registry-observed digests after pull or inspection. An accepted run
must not install code from a branch or `latest` tag.

Exit condition: all images, model inputs, dataset inputs, workflow parameters,
and prompt/evaluation policies have immutable identities.

## 5. Gate G1: H200 node, driver, and container runtime

First inspect Kubernetes without changing the cluster:

```bash
kubectl get nodes -o wide | tee "${EVIDENCE_DIR}/cluster/nodes.txt"
kubectl get nodes -L "${NODE_SELECTOR_KEY}" \
  | tee "${EVIDENCE_DIR}/cluster/node-labels.txt"

gpu_node=$(kubectl get nodes \
  -l "${NODE_SELECTOR_KEY}=${NODE_SELECTOR_VALUE}" \
  -o jsonpath='{.items[0].metadata.name}')
test -n "${gpu_node}"

kubectl get node "${gpu_node}" -o yaml \
  > "${EVIDENCE_DIR}/cluster/gpu-node.yaml"
```

On the H200 host, capture `nvidia-smi -q`, driver packages, containerd
configuration, and NVIDIA Container Toolkit versions using the site's
approved node-access method. On an Ubuntu node that does not yet have the
driver/toolkit stack, use the repository installer and reboot before
continuing:

```bash
sudo env CONTAINER_RUNTIME=containerd \
  EGRESS_PROXY="${EGRESS_PROXY:-}" \
  ./scripts/bootstrap_gpu_runtime.sh
sudo systemctl reboot
```

The installer preserves an existing Ubuntu server-open driver branch, installs
its matching `nvidia-utils` and Fabric Manager packages, installs the target
kernel's headers, preserves its `linux-modules-extra` package for InfiniBand,
verifies the NVIDIA kernel module, and configures NVIDIA as containerd's
default runtime. It records the selected runtime, package contract, and
whether a kubelet Memory Manager checkpoint was present in
`/var/lib/ai-build-tools/runtime-install.json`.

If the install record reports a Memory Manager checkpoint, complete this only
after the node is drained and the absence of user workloads is recorded:

```bash
sudo systemctl stop kubelet
sudo install -D -m 0600 /var/lib/kubelet/memory_manager_state \
  /var/lib/ai-build-tools/evidence/memory_manager_state.before-gpu-driver
sudo rm /var/lib/kubelet/memory_manager_state
sudo systemctl reboot
```

The checkpoint contains topology state from before the driver was loaded.
Kubelet recreates it against the post-driver NUMA map after reboot. Never
remove this state from an undrained node; Kubernetes can reject the old state
with `the expected machine state is different from the real one`.

Driver installation and reboot are disruptive node operations. Drain or
otherwise protect running workloads before using this path on an existing
cluster node. Do not restart containerd underneath active workloads and call
the result an accepted run.

Exit condition: the host sees exactly the expected H200 devices, Fabric
Manager/driver health is accepted by the site, and containerd has a working
NVIDIA runtime integration.

## 6. Gate G2: NVIDIA device plugin and allocatable GPUs

Prefer the site's managed NVIDIA GPU Operator or device-plugin deployment. If
the cluster has neither, select a reviewed NVIDIA device-plugin release, pin
that version in the run record, and install its Helm chart:

```bash
export NVIDIA_DEVICE_PLUGIN_VERSION=REPLACE_WITH_REVIEWED_VERSION
chart_url=https://nvidia.github.io/k8s-device-plugin/stable/\
nvidia-device-plugin-${NVIDIA_DEVICE_PLUGIN_VERSION#v}.tgz

kubectl label node "${gpu_node}" nvidia.com/gpu.present=true --overwrite
cat > "${EVIDENCE_DIR}/cluster/device-plugin-values.yaml" <<'EOF'
nfd:
  enabled: false
gfd:
  enabled: false
tolerations:
  - key: CriticalAddonsOnly
    operator: Exists
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  - key: node-role.kubernetes.io/control-plane
    operator: Exists
    effect: NoSchedule
EOF

helm upgrade --install nvidia-device-plugin \
  --namespace nvidia-device-plugin \
  --create-namespace \
  --values "${EVIDENCE_DIR}/cluster/device-plugin-values.yaml" \
  "${chart_url}"

plugin_daemonset=$(kubectl get daemonset -n nvidia-device-plugin \
  -l app.kubernetes.io/name=nvidia-device-plugin \
  -o jsonpath='{.items[0].metadata.name}')
test -n "${plugin_daemonset}"
kubectl rollout status "daemonset/${plugin_daemonset}" \
  -n nvidia-device-plugin --timeout=5m
```

Record the deployed chart and image identities, then require the expected
capacity and allocatable values:

```bash
kubectl get pods -n nvidia-device-plugin -o wide \
  | tee "${EVIDENCE_DIR}/cluster/device-plugin-pods.txt"
helm get all nvidia-device-plugin -n nvidia-device-plugin \
  > "${EVIDENCE_DIR}/cluster/device-plugin-release.txt"

capacity=$(kubectl get node "${gpu_node}" \
  -o jsonpath='{.status.capacity.nvidia\.com/gpu}')
allocatable=$(kubectl get node "${gpu_node}" \
  -o jsonpath='{.status.allocatable.nvidia\.com/gpu}')
test "${capacity}" -eq "${EXPECTED_NODE_GPU_COUNT}"
test "${allocatable}" -eq "${EXPECTED_NODE_GPU_COUNT}"
```

Exit condition: the selected node advertises exactly the expected physical
GPU count. A node label without this extended resource fails the gate.

The two smoke images have separate contracts. `CUDA_SMOKE_IMAGE` must contain
`nvidia-smi`. `PYTORCH_CUDA_IMAGE` must contain a CUDA-enabled PyTorch build;
using a CPU-only wheel is a gate failure. Pin both final image references by
digest even when a tag was used during image development.

## 7. Gate G3: one-GPU CUDA smoke

Render the templates after checking that all required variables are set:

```bash
: "${WORKLOAD_NAMESPACE:?}"
: "${NODE_SELECTOR_KEY:?}"
: "${NODE_SELECTOR_VALUE:?}"
: "${CUDA_SMOKE_IMAGE:?}"
: "${PYTORCH_CUDA_IMAGE:?}"

kubectl create namespace "${WORKLOAD_NAMESPACE}" --dry-run=client -o yaml \
  | kubectl apply -f -

envsubst < kubernetes-CUDA/templates/nvidia-smi-pod.yaml \
  > "${EVIDENCE_DIR}/smoke/nvidia-smi-pod.yaml"
kubectl apply --dry-run=server \
  -f "${EVIDENCE_DIR}/smoke/nvidia-smi-pod.yaml"
kubectl apply -f "${EVIDENCE_DIR}/smoke/nvidia-smi-pod.yaml"
kubectl wait -n "${WORKLOAD_NAMESPACE}" \
  --for=jsonpath='{.status.phase}'=Succeeded \
  pod/ai-build-tools-nvidia-smi --timeout=5m
kubectl logs -n "${WORKLOAD_NAMESPACE}" ai-build-tools-nvidia-smi \
  | tee "${EVIDENCE_DIR}/smoke/nvidia-smi.log"
grep -F "${EXPECTED_GPU_MODEL}" \
  "${EVIDENCE_DIR}/smoke/nvidia-smi.log"
kubectl get pod -n "${WORKLOAD_NAMESPACE}" ai-build-tools-nvidia-smi -o yaml \
  > "${EVIDENCE_DIR}/smoke/nvidia-smi-observed.yaml"
observed_node=$(kubectl get pod -n "${WORKLOAD_NAMESPACE}" \
  ai-build-tools-nvidia-smi -o jsonpath='{.spec.nodeName}')
test "${observed_node}" = "${gpu_node}"

envsubst < kubernetes-CUDA/templates/pytorch-cuda-job.yaml \
  > "${EVIDENCE_DIR}/smoke/pytorch-cuda-job.yaml"
kubectl apply --dry-run=server \
  -f "${EVIDENCE_DIR}/smoke/pytorch-cuda-job.yaml"
kubectl apply -f "${EVIDENCE_DIR}/smoke/pytorch-cuda-job.yaml"
kubectl wait -n "${WORKLOAD_NAMESPACE}" --for=condition=complete \
  job/ai-build-tools-pytorch-cuda --timeout=10m
kubectl logs -n "${WORKLOAD_NAMESPACE}" job/ai-build-tools-pytorch-cuda \
  | tee "${EVIDENCE_DIR}/smoke/pytorch-cuda.log"
grep -F "${EXPECTED_GPU_MODEL}" \
  "${EVIDENCE_DIR}/smoke/pytorch-cuda.log"
kubectl get job -n "${WORKLOAD_NAMESPACE}" \
  ai-build-tools-pytorch-cuda -o yaml \
  > "${EVIDENCE_DIR}/smoke/pytorch-cuda-observed.yaml"
pytorch_pod=$(kubectl get pod -n "${WORKLOAD_NAMESPACE}" \
  -l job-name=ai-build-tools-pytorch-cuda \
  -o jsonpath='{.items[0].metadata.name}')
test -n "${pytorch_pod}"
test "$(kubectl get pod -n "${WORKLOAD_NAMESPACE}" "${pytorch_pod}" \
  -o jsonpath='{.spec.nodeName}')" = "${gpu_node}"
kubectl get pod -n "${WORKLOAD_NAMESPACE}" "${pytorch_pod}" -o yaml \
  > "${EVIDENCE_DIR}/smoke/pytorch-cuda-pod-observed.yaml"
```

Exit condition: both workloads request one `nvidia.com/gpu`, run on the
selected physical node, and succeed. The PyTorch log must report an H200-class
device and a successful tensor operation.

Before beginning training, run a whole-node allocation smoke. This is stronger
than a capacity label but is not a substitute for the DDP/NCCL evidence in G5:

```bash
envsubst < kubernetes-CUDA/templates/pytorch-all-gpu-job.yaml \
  > "${EVIDENCE_DIR}/smoke/pytorch-all-gpu-job.yaml"
kubectl apply --dry-run=server \
  -f "${EVIDENCE_DIR}/smoke/pytorch-all-gpu-job.yaml"
kubectl apply -f "${EVIDENCE_DIR}/smoke/pytorch-all-gpu-job.yaml"
kubectl wait -n "${WORKLOAD_NAMESPACE}" --for=condition=complete \
  job/ai-build-tools-pytorch-all-gpu --timeout=10m
kubectl logs -n "${WORKLOAD_NAMESPACE}" \
  job/ai-build-tools-pytorch-all-gpu \
  | tee "${EVIDENCE_DIR}/smoke/pytorch-all-gpu.log"
grep -F "\"device_count\": ${TRAIN_GPU_COUNT}" \
  "${EVIDENCE_DIR}/smoke/pytorch-all-gpu.log"
kubectl get job -n "${WORKLOAD_NAMESPACE}" \
  ai-build-tools-pytorch-all-gpu -o yaml \
  > "${EVIDENCE_DIR}/smoke/pytorch-all-gpu-observed.yaml"
```

Exit condition: one pod receives `${TRAIN_GPU_COUNT}` GPUs and completes a
CUDA operation on each visible device. Keep the observed device list as
evidence. Do not label this `single-node-ddp`; it has not launched ranks or
qualified NCCL collectives.

## 8. Gates G4-G5: one-GPU pilot, then eight-GPU training

Run a small one-GPU pilot first. It must materialize the exact model and
dataset inputs, execute forward/backward passes, save a loadable LoRA, and
generate one image. Keep the pilot artifact separate from a release.

The full run then requests `${TRAIN_GPU_COUNT}` GPUs on one pod and launches
one process per GPU. For an eight-GPU node, the launcher must be explicit, for
example:

```text
accelerate launch --multi_gpu --num_processes 8 <pinned trainer and arguments>
```

Do not accept a run merely because its pod limits request eight GPUs. Require
runtime evidence containing all of the following:

- one pod scheduled to the selected H200 node;
- pod limit `nvidia.com/gpu: 8`;
- `WORLD_SIZE=8` and ranks `0..7`;
- eight distinct GPU UUIDs mapped to the ranks;
- no rank failure or NCCL error;
- final LoRA digest and trainer metrics; and
- the ability to load the produced LoRA in a new process.

Exit condition: candidate A was produced by the observed eight-rank run and
its adapter digest is independently verified.

## 9. Gates G6-G7: immutable A and derived B

Promote A only after generation, evaluation, and a fresh load pass. Its model
record must include at least:

- immutable base revision/digest;
- dataset digest;
- training image digest and canonical parameters;
- adapter URI and digest;
- KFP run identity;
- observed GPU/rank evidence;
- generated-output digests and evaluation verdict; and
- lifecycle state `RELEASED`.

Start B under a new KFP run ID. The resolver must download or mount release A,
verify its recorded digest, and expose it read-only. The training process must
load A's LoRA state before executing B's first optimization step. Record A as
B's parent release and package B under a new immutable URI. Never modify A.

Exit condition: Hub shows B's parent as A, B has a distinct adapter digest,
and the run evidence proves that A was loaded before B training began.

## 10. Gate G8: CUDA KServe validation

Use KServe Standard deployment mode when full Kubernetes GPU resource and
placement control is needed. The generated `InferenceService` must include:

```yaml
spec:
  predictor:
    nodeSelector:
      ai-build-tools.ricolin.dev/accelerator: nvidia-h200
    containers:
      - name: kserve-container
        image: registry.example.com/ai-build-tools-cuda-serving@sha256:<digest>
        resources:
          limits:
            nvidia.com/gpu: "1"
```

The final manifest may also need site-owned tolerations, storage credentials,
and ingress settings. Do not put credentials in the model record or evidence.

Delete the earlier predictor pod, wait for a newly created pod UID, and send a
request from a separate client process. Require the response or runtime status
to identify the verified base and B adapter digests. Record:

```bash
kubectl get inferenceservice -n "${WORKLOAD_NAMESPACE}" -o yaml \
  > "${EVIDENCE_DIR}/serving/inferenceservices.yaml"
kubectl get pods -n "${WORKLOAD_NAMESPACE}" -o wide \
  > "${EVIDENCE_DIR}/serving/pods.txt"
kubectl get pods -n "${WORKLOAD_NAMESPACE}" -o yaml \
  > "${EVIDENCE_DIR}/serving/pods.yaml"
kubectl logs -n "${WORKLOAD_NAMESPACE}" <fresh-predictor-pod> \
  > "${EVIDENCE_DIR}/serving/predictor.log"
```

Exit condition: a fresh predictor requested one GPU, ran on the physical H200
node, verified both artifact digests, became Ready, and returned a valid image
whose digest is recorded.

## 11. Gate G9: evidence, replay, and acceptance

Copy KFP run manifests, pod specs/status, container logs, Hub records, KServe
objects, release manifests, and artifact checksums into the unique evidence
directory. Generate checksums last:

```bash
find "${EVIDENCE_DIR}" -type f ! -name SHA256SUMS -print0 \
  | sort -z \
  | xargs -0 sha256sum \
  > "${EVIDENCE_DIR}/SHA256SUMS"

find "${EVIDENCE_DIR}" -type f -print0 \
  | xargs -0 grep -IlE \
      'token|password|secret|BEGIN .*PRIVATE KEY' || true
```

The grep result is a review queue, not an automatic secret verdict. Inspect
and redact sensitive files before publishing evidence; regenerate checksums
after any redaction.

An end-to-end physical release passes only when:

- all G0-G8 gates passed in order;
- the selected node advertised eight physical GPUs;
- one-GPU smoke, pilot, generation, and serving passed;
- the full A training run proved eight ranks on eight distinct GPUs;
- A is immutable and released;
- B was trained from the verified A adapter under a new run;
- B is immutable and released with `parent=A`;
- a fresh KServe predictor loaded B and served one valid output;
- all images and artifacts have immutable digests;
- no credential appears in retained evidence; and
- re-running verification from the retained release records succeeds.

If any condition fails, retain the failed run as evidence, assign a new run ID
to the retry, and report only the highest proof state that actually passed.

## 12. Cleanup

Preserve release records and the evidence directory. Remove only validation
workloads owned by this run:

```bash
kubectl delete pod -n "${WORKLOAD_NAMESPACE}" \
  ai-build-tools-nvidia-smi --ignore-not-found
kubectl delete job -n "${WORKLOAD_NAMESPACE}" \
  ai-build-tools-pytorch-cuda --ignore-not-found
```

Do not uninstall a site-managed NVIDIA device plugin, Kubeflow, KServe,
storage system, or shared namespace as part of run cleanup.

## 13. Repository checks before a validation run

Run these checks after changing the templates or workflow code:

```bash
yamllint kubernetes-CUDA/templates/*.yaml
bash -n kubernetes-CUDA/templates/h200-validation.env.example

uv sync --project kubernetes/workflows --python 3.12 --frozen
uv run --project kubernetes/workflows --frozen \
  pytest -q kubernetes/workflows/tests

git diff --check
```

`envsubst` is supplied by `gettext-base` on Ubuntu. Source YAML linting is an
offline check. The server-side dry runs in G3 are the authoritative schema and
admission checks against the target cluster.
