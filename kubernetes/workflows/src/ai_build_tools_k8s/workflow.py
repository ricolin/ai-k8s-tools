from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import struct
import subprocess
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Any


CONTROL_PLANE_TOLERATIONS = (
    {
        "key": "node-role.kubernetes.io/control-plane",
        "operator": "Exists",
        "effect": "NoSchedule",
    },
    {
        "key": "node-role.kubernetes.io/master",
        "operator": "Exists",
        "effect": "NoSchedule",
    },
)


def add_control_plane_tolerations(pod_spec: dict[str, Any], enabled: bool) -> None:
    if enabled:
        pod_spec["tolerations"] = [dict(toleration) for toleration in CONTROL_PLANE_TOLERATIONS]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_http_endpoint(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return value
    return f"http://{value}"


def normalize_artifact_uri(value: str) -> str:
    if value.startswith("minio://"):
        return f"s3://{value.removeprefix('minio://')}"
    return value


def hub_version_exists(client: Any, model_name: str, model_version: str) -> bool:
    if client.get_registered_model(model_name) is None:
        return False
    return client.get_model_version(model_name, model_version) is not None


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        return sha256_file(path)
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def artifact_digest(path: Path) -> str:
    adapter = path / "pytorch_lora_weights.safetensors" if path.is_dir() else None
    return sha256_file(adapter) if adapter is not None and adapter.is_file() else sha256_tree(path)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def write_deterministic_png(path: Path, seed_material: bytes, width: int = 64, height: int = 64) -> None:
    seed = hashlib.sha256(seed_material).digest()
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            pixel = hashlib.sha256(seed + struct.pack(">II", x, y)).digest()
            rows.extend(pixel[:3])
    payload = b"\x89PNG\r\n\x1a\n"
    payload += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    payload += _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    payload += _png_chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def resolve_inputs(args: argparse.Namespace) -> None:
    resolved = {
        "schema_version": "0.1.0",
        "base_model": {"ref": args.base_model_ref, "revision": args.base_model_revision},
        "dataset": {"digest": args.dataset_digest},
        "parent": {"uri": args.parent_uri, "digest": args.parent_digest} if args.parent_uri else None,
        "profile": args.profile,
        "evidence_class": args.evidence_class,
        "evidence_level": args.evidence_level,
    }
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "resolved-inputs.json", resolved)

    parent = Path(args.parent_output)
    parent.mkdir(parents=True, exist_ok=True)
    if args.parent_path:
        source = Path(args.parent_path)
        if not source.exists():
            raise SystemExit(f"parent artifact path does not exist: {source}")
        if source.is_dir():
            for item in source.iterdir():
                target = parent / item.name
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
        else:
            shutil.copy2(source, parent / source.name)
        observed = artifact_digest(parent)
        if args.parent_digest and observed != args.parent_digest.removeprefix("sha256:"):
            raise SystemExit(f"parent digest mismatch: expected {args.parent_digest}, observed sha256:{observed}")
    elif args.parent_uri:
        if not args.parent_uri.startswith("s3://"):
            raise SystemExit(f"only s3:// parent URIs are supported: {args.parent_uri}")
        import boto3

        bucket_and_prefix = args.parent_uri.removeprefix("s3://")
        bucket, _, prefix = bucket_and_prefix.partition("/")
        client = boto3.client("s3")
        objects = client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
        if not objects:
            raise SystemExit(f"parent artifact URI has no objects: {args.parent_uri}")
        for entry in objects:
            key = entry["Key"]
            relative = key[len(prefix) :].lstrip("/") or Path(key).name
            destination = parent / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(destination))
        observed = artifact_digest(parent)
        if args.parent_digest and observed != args.parent_digest.removeprefix("sha256:"):
            raise SystemExit(f"parent digest mismatch: expected {args.parent_digest}, observed sha256:{observed}")
    else:
        write_json(parent / "base-model.json", resolved["base_model"])


def train_fixture(args: argparse.Namespace) -> None:
    parent = Path(args.parent)
    parent_digest = artifact_digest(parent)
    adapter_payload = {
        "format": "ai-build-tools-mechanics-adapter",
        "model_family": "sdxl-lora-fixture",
        "parent_digest": f"sha256:{parent_digest}",
        "dataset_digest": args.dataset_digest,
        "steps": args.steps,
        "rank": args.rank,
        "seed": args.seed,
        "run_id": args.run_id,
    }
    adapter = Path(args.adapter)
    adapter.mkdir(parents=True, exist_ok=True)
    adapter_file = adapter / "pytorch_lora_weights.safetensors"
    adapter_file.write_bytes(b"AI_BUILD_TOOLS_MECHANICS_ONLY\n" + canonical_json(adapter_payload) + b"\n")
    adapter_digest = sha256_file(adapter_file)
    metrics = {
        "schema_version": "0.1.0",
        "status": "success",
        "steps": args.steps,
        "adapter_sha256": adapter_digest,
        "parent_sha256": parent_digest,
        "fixture_loss": round(int(adapter_digest[:8], 16) / 0xFFFFFFFF, 8),
    }
    metrics_dir = Path(args.metrics)
    metrics_dir.mkdir(parents=True, exist_ok=True)
    write_json(metrics_dir / "metrics.json", metrics)


def generate_fixture(args: argparse.Namespace) -> None:
    adapter = Path(args.adapter)
    adapter_file = adapter / "pytorch_lora_weights.safetensors"
    if not adapter_file.is_file():
        raise SystemExit(f"adapter is missing: {adapter_file}")
    adapter_digest = sha256_file(adapter_file)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    images = []
    for index in range(args.count):
        image = output / f"generated-{index:02d}.png"
        material = canonical_json(
            {"adapter": adapter_digest, "prompt": args.prompt, "seed": args.seed + index}
        )
        write_deterministic_png(image, material)
        images.append(
            {
                "file": image.name,
                "sha256": sha256_file(image),
                "prompt": args.prompt,
                "seed": args.seed + index,
                "width": 64,
                "height": 64,
            }
        )
    write_json(output / "metadata.json", images)


def evaluate_fixture(args: argparse.Namespace) -> None:
    adapter_file = Path(args.adapter) / "pytorch_lora_weights.safetensors"
    images_dir = Path(args.images)
    metadata = json.loads((images_dir / "metadata.json").read_text())
    errors: list[str] = []
    hashes: set[str] = set()
    if not adapter_file.is_file():
        errors.append("adapter missing")
    for record in metadata:
        image = images_dir / record["file"]
        if not image.is_file():
            errors.append(f"missing image {record['file']}")
            continue
        observed = sha256_file(image)
        if observed != record["sha256"]:
            errors.append(f"image digest mismatch {record['file']}")
        if observed in hashes:
            errors.append(f"duplicate image digest {observed}")
        hashes.add(observed)
        if image.stat().st_size < 256:
            errors.append(f"image too small {record['file']}")
    if len(metadata) != args.expected_images:
        errors.append(f"expected {args.expected_images} images, observed {len(metadata)}")
    report = {
        "schema_version": "0.1.0",
        "pass": not errors,
        "errors": errors,
        "adapter_sha256": sha256_file(adapter_file) if adapter_file.is_file() else None,
        "image_count": len(metadata),
    }
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "evaluation.json", report)
    if errors:
        raise SystemExit("; ".join(errors))


def register_candidate(args: argparse.Namespace) -> None:
    from model_registry import ModelRegistry

    adapter_file = Path(args.adapter) / "pytorch_lora_weights.safetensors"
    evaluation = json.loads((Path(args.evaluation) / "evaluation.json").read_text())
    if not evaluation["pass"]:
        raise SystemExit("refusing to register a failed candidate")
    adapter_digest = sha256_file(adapter_file)
    adapter_uri = normalize_artifact_uri(args.adapter_uri)
    base_artifact_uri = normalize_artifact_uri(args.base_artifact_uri)
    metadata = {
        "lifecycle_status": "CANDIDATE",
        "adapter_sha256": adapter_digest,
        "base_artifact_uri": base_artifact_uri,
        "base_model_ref": args.base_model_ref,
        "base_model_revision": args.base_model_revision,
        "dataset_digest": args.dataset_digest,
        "parent_model_version": args.parent_model_version,
        "pipeline_run_id": args.run_id,
        "evidence_class": args.evidence_class,
        "evidence_level": args.evidence_level,
    }
    client = ModelRegistry(
        server_address=normalize_http_endpoint(args.registry_host),
        port=args.registry_port,
        author=args.author,
        is_secure=False,
    )
    if hub_version_exists(client, args.model_name, args.model_version):
        raise SystemExit(f"refusing to overwrite Hub model version {args.model_name}/{args.model_version}")
    client.register_model(
        args.model_name,
        adapter_uri,
        model_format_name="safetensors",
        model_format_version="fixture-v1",
        version=args.model_version,
        description="Kubeflow mechanics candidate; not a physical GPU release",
        metadata=metadata,
    )
    manifest = {
        "schema_version": "0.1.0",
        "status": "CANDIDATE",
        "model_name": args.model_name,
        "model_version": args.model_version,
        "base_model": {"ref": args.base_model_ref, "revision": args.base_model_revision},
        "base_artifact_uri": base_artifact_uri,
        "adapter": {"uri": adapter_uri, "sha256": adapter_digest},
        "parent_model_version": args.parent_model_version or None,
        "dataset_digest": args.dataset_digest,
        "pipeline_run_id": args.run_id,
        "evaluation": evaluation,
        "evidence_class": args.evidence_class,
        "evidence_level": args.evidence_level,
    }
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "candidate.json", manifest)


def render_inference_service(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {
            "name": args.service_name,
            "namespace": args.namespace,
            "annotations": {
                "serving.kserve.io/deploymentMode": "Standard",
                "ai-build-tools.ricolin.dev/evidence-class": args.evidence_class,
            },
        },
        "spec": {
            "predictor": {
                "serviceAccountName": args.service_account,
                "storageUris": [
                    {"uri": args.base_uri, "mountPath": "/mnt/models/base"},
                    {"uri": args.adapter_uri, "mountPath": "/mnt/models/adapter"},
                ],
                "containers": [
                    {
                        "name": "kserve-container",
                        "image": args.runtime_image,
                        "imagePullPolicy": "IfNotPresent",
                        "args": [
                            "--model-name",
                            args.service_name,
                            "--base-dir",
                            "/mnt/models/base",
                            "--adapter-dir",
                            "/mnt/models/adapter",
                            "--port",
                            "8080",
                        ],
                        "ports": [{"name": "http1", "containerPort": 8080, "protocol": "TCP"}],
                        "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}},
                        "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "128Mi"},
                            "limits": {"cpu": "1", "memory": "512Mi"},
                        },
                    }
                ],
            }
        },
    }


def deploy_inference(args: argparse.Namespace) -> None:
    manifest = render_inference_service(args)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest_file = output / "inferenceservice.json"
    write_json(manifest_file, manifest)
    subprocess.run(["kubectl", "apply", "-f", str(manifest_file)], check=True)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "kubectl",
                "-n",
                args.namespace,
                "get",
                "inferenceservice",
                args.service_name,
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        resource = json.loads(result.stdout)
        conditions = resource.get("status", {}).get("conditions", [])
        if any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions):
            write_json(output / "inferenceservice.observed.json", resource)
            return
        time.sleep(5)
    raise SystemExit(f"InferenceService {args.namespace}/{args.service_name} did not become ready")


def request_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = canonical_json(payload) if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify_inference(args: argparse.Namespace) -> None:
    base_url = f"http://{args.service_name}-predictor.{args.namespace}.svc.cluster.local"
    ready = request_json(f"{base_url}/readyz")
    prediction = request_json(
        f"{base_url}/v1/models/{args.service_name}:predict",
        {"instances": [{"prompt": args.prompt, "seed": args.seed}]},
    )
    encoded = prediction["predictions"][0]["image_base64"]
    image = base64.b64decode(encoded, validate=True)
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SystemExit("inference response is not a PNG")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "prediction.png").write_bytes(image)
    report = {
        "schema_version": "0.1.0",
        "pass": True,
        "ready": ready,
        "model_name": prediction.get("model_name"),
        "model_digest": prediction.get("model_digest"),
        "image_sha256": hashlib.sha256(image).hexdigest(),
    }
    write_json(output / "deployment-verification.json", report)


def promote_model(args: argparse.Namespace) -> None:
    from model_registry import ModelRegistry

    verification = json.loads((Path(args.verification) / "deployment-verification.json").read_text())
    if not verification["pass"]:
        raise SystemExit("refusing to release an unverified deployment")
    client = ModelRegistry(
        server_address=normalize_http_endpoint(args.registry_host),
        port=args.registry_port,
        author=args.author,
        is_secure=False,
    )
    version = client.get_model_version(args.model_name, args.model_version)
    if version is None:
        raise SystemExit(f"Hub model version does not exist: {args.model_name}/{args.model_version}")
    properties = dict(version.custom_properties or {})
    if properties.get("lifecycle_status") == "RELEASED":
        raise SystemExit("model version is already released")
    properties.update(
        {
            "lifecycle_status": "RELEASED",
            "kserve_namespace": args.namespace,
            "kserve_service": args.service_name,
            "deployment_image_sha256": verification["image_sha256"],
        }
    )
    version.custom_properties = properties
    client.update(version)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    write_json(
        output / "release.json",
        {
            "schema_version": "0.1.0",
            "status": "RELEASED",
            "model_name": args.model_name,
            "model_version": args.model_version,
            "service": f"{args.namespace}/{args.service_name}",
            "verification": verification,
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve")
    resolve.add_argument("--base-model-ref", required=True)
    resolve.add_argument("--base-model-revision", required=True)
    resolve.add_argument("--dataset-digest", required=True)
    resolve.add_argument("--parent-uri", default="")
    resolve.add_argument("--parent-digest", default="")
    resolve.add_argument("--parent-path", default="")
    resolve.add_argument("--profile", required=True)
    resolve.add_argument("--evidence-class", required=True)
    resolve.add_argument("--evidence-level", required=True)
    resolve.add_argument("--output", required=True)
    resolve.add_argument("--parent-output", required=True)
    resolve.set_defaults(handler=resolve_inputs)

    train = commands.add_parser("train-fixture")
    train.add_argument("--parent", required=True)
    train.add_argument("--adapter", required=True)
    train.add_argument("--metrics", required=True)
    train.add_argument("--dataset-digest", required=True)
    train.add_argument("--steps", type=int, required=True)
    train.add_argument("--rank", type=int, required=True)
    train.add_argument("--seed", type=int, required=True)
    train.add_argument("--run-id", required=True)
    train.set_defaults(handler=train_fixture)

    generate = commands.add_parser("generate-fixture")
    generate.add_argument("--adapter", required=True)
    generate.add_argument("--output", required=True)
    generate.add_argument("--prompt", required=True)
    generate.add_argument("--seed", type=int, required=True)
    generate.add_argument("--count", type=int, default=3)
    generate.set_defaults(handler=generate_fixture)

    evaluate = commands.add_parser("evaluate-fixture")
    evaluate.add_argument("--adapter", required=True)
    evaluate.add_argument("--images", required=True)
    evaluate.add_argument("--output", required=True)
    evaluate.add_argument("--expected-images", type=int, default=3)
    evaluate.set_defaults(handler=evaluate_fixture)

    register = commands.add_parser("register-candidate")
    register.add_argument("--adapter", required=True)
    register.add_argument("--adapter-uri", required=True)
    register.add_argument("--base-artifact-uri", required=True)
    register.add_argument("--evaluation", required=True)
    register.add_argument("--output", required=True)
    register.add_argument("--model-name", required=True)
    register.add_argument("--model-version", required=True)
    register.add_argument("--parent-model-version", default="")
    register.add_argument("--base-model-ref", required=True)
    register.add_argument("--base-model-revision", required=True)
    register.add_argument("--dataset-digest", required=True)
    register.add_argument("--run-id", required=True)
    register.add_argument("--evidence-class", required=True)
    register.add_argument("--evidence-level", required=True)
    register.add_argument("--registry-host", required=True)
    register.add_argument("--registry-port", type=int, default=8080)
    register.add_argument("--author", default="ai-build-tools")
    register.set_defaults(handler=register_candidate)

    deploy = commands.add_parser("deploy")
    deploy.add_argument("--service-name", required=True)
    deploy.add_argument("--namespace", required=True)
    deploy.add_argument("--service-account", required=True)
    deploy.add_argument("--base-uri", required=True)
    deploy.add_argument("--adapter-uri", required=True)
    deploy.add_argument("--runtime-image", required=True)
    deploy.add_argument("--evidence-class", required=True)
    deploy.add_argument("--output", required=True)
    deploy.add_argument("--timeout", type=int, default=600)
    deploy.set_defaults(handler=deploy_inference)

    verify = commands.add_parser("verify")
    verify.add_argument("--service-name", required=True)
    verify.add_argument("--namespace", required=True)
    verify.add_argument("--prompt", required=True)
    verify.add_argument("--seed", type=int, required=True)
    verify.add_argument("--output", required=True)
    verify.set_defaults(handler=verify_inference)

    promote = commands.add_parser("promote")
    promote.add_argument("--verification", required=True)
    promote.add_argument("--output", required=True)
    promote.add_argument("--model-name", required=True)
    promote.add_argument("--model-version", required=True)
    promote.add_argument("--namespace", required=True)
    promote.add_argument("--service-name", required=True)
    promote.add_argument("--registry-host", required=True)
    promote.add_argument("--registry-port", type=int, default=8080)
    promote.add_argument("--author", default="ai-build-tools")
    promote.set_defaults(handler=promote_model)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
