from __future__ import annotations

import argparse
import base64
import json
import re
import threading
import urllib.request
from pathlib import Path

import pytest
from kfp import compiler

from ai_build_tools_k8s.pipeline import make_deployment_pipeline, make_training_pipeline
from ai_build_tools_k8s.server import ModelServer, handler_factory
from ai_build_tools_k8s.workflow import (
    artifact_digest,
    evaluate_fixture,
    generate_fixture,
    hub_version_exists,
    normalize_artifact_uri,
    normalize_http_endpoint,
    render_inference_service,
    resolve_inputs,
    sha256_file,
    train_fixture,
)
from http.server import ThreadingHTTPServer


def namespace(**values):
    return argparse.Namespace(**values)


def make_parent(path: Path) -> None:
    path.mkdir()
    (path / "base-model.json").write_text('{"revision":"pinned"}\n')


def train(tmp_path: Path, parent: Path, run_id: str = "run-a") -> tuple[Path, Path]:
    adapter = tmp_path / f"adapter-{run_id}"
    metrics = tmp_path / f"metrics-{run_id}"
    train_fixture(
        namespace(
            parent=str(parent),
            adapter=str(adapter),
            metrics=str(metrics),
            dataset_digest="sha256:dataset",
            steps=12,
            rank=4,
            seed=7,
            run_id=run_id,
        )
    )
    return adapter, metrics


def test_initial_training_is_deterministic(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    make_parent(parent)
    first, _ = train(tmp_path, parent, "same")
    digest = sha256_file(first / "pytorch_lora_weights.safetensors")
    second, _ = train(tmp_path, parent, "same")
    assert sha256_file(second / "pytorch_lora_weights.safetensors") == digest


def test_registry_endpoint_always_has_a_scheme() -> None:
    assert normalize_http_endpoint("model-registry-service.kubeflow") == (
        "http://model-registry-service.kubeflow"
    )
    assert normalize_http_endpoint("https://registry.example") == "https://registry.example"


def test_kfp_minio_uri_is_canonicalized_for_kserve() -> None:
    assert normalize_artifact_uri("minio://mlpipeline/v2/artifacts/model") == (
        "s3://mlpipeline/v2/artifacts/model"
    )
    assert normalize_artifact_uri("s3://release-bucket/model") == "s3://release-bucket/model"


def test_new_hub_model_is_not_treated_as_an_overwrite() -> None:
    class EmptyHub:
        def get_registered_model(self, _name: str):
            return None

        def get_model_version(self, _name: str, _version: str):
            raise AssertionError("version lookup must not run for a new model")

    assert hub_version_exists(EmptyHub(), "new-model", "v1") is False


def test_derived_training_changes_adapter_and_records_parent(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    make_parent(parent)
    adapter_a, _ = train(tmp_path, parent, "a")
    adapter_b, metrics_b = train(tmp_path, adapter_a, "b")
    assert artifact_digest(adapter_a) != artifact_digest(adapter_b)
    metrics = json.loads((metrics_b / "metrics.json").read_text())
    assert metrics["parent_sha256"] == artifact_digest(adapter_a)


def test_generate_and_evaluate(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    make_parent(parent)
    adapter, _ = train(tmp_path, parent)
    images = tmp_path / "images"
    generate_fixture(namespace(adapter=str(adapter), output=str(images), prompt="cute bear", seed=5, count=3))
    report = tmp_path / "report"
    evaluate_fixture(
        namespace(adapter=str(adapter), images=str(images), output=str(report), expected_images=3)
    )
    assert json.loads((report / "evaluation.json").read_text())["pass"] is True


def test_evaluate_rejects_duplicate_images(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    make_parent(parent)
    adapter, _ = train(tmp_path, parent)
    images = tmp_path / "images"
    generate_fixture(namespace(adapter=str(adapter), output=str(images), prompt="cute bear", seed=5, count=3))
    metadata_path = images / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    (images / metadata[2]["file"]).write_bytes((images / metadata[1]["file"]).read_bytes())
    metadata[2]["sha256"] = metadata[1]["sha256"]
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(SystemExit, match="duplicate image digest"):
        evaluate_fixture(
            namespace(adapter=str(adapter), images=str(images), output=str(tmp_path / "report"), expected_images=3)
        )


def test_resolve_rejects_wrong_parent_digest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_parent(source)
    with pytest.raises(SystemExit, match="parent digest mismatch"):
        resolve_inputs(
            namespace(
                base_model_ref="sdxl",
                base_model_revision="pinned",
                dataset_digest="sha256:dataset",
                parent_uri="s3://bucket/adapter",
                parent_digest="sha256:wrong",
                parent_path=str(source),
                profile="test",
                evidence_class="emulated",
                evidence_level="mechanics",
                output=str(tmp_path / "resolved"),
                parent_output=str(tmp_path / "parent"),
            )
        )


def test_inference_service_has_separate_base_and_adapter_mounts() -> None:
    manifest = render_inference_service(
        namespace(
            service_name="cute-bear",
            namespace="ai-workflows",
            service_account="ai-build-tools-serving",
            base_uri="s3://bucket/base",
            adapter_uri="s3://bucket/adapter",
            runtime_image="registry/runtime@sha256:abc",
            evidence_class="emulated-mcapi",
        )
    )
    uris = manifest["spec"]["predictor"]["storageUris"]
    assert uris == [
        {"uri": "s3://bucket/base", "mountPath": "/mnt/models/base"},
        {"uri": "s3://bucket/adapter", "mountPath": "/mnt/models/adapter"},
    ]
    assert manifest["metadata"]["annotations"]["serving.kserve.io/deploymentMode"] == "Standard"


def test_pipeline_defaults_are_provider_neutral(tmp_path: Path) -> None:
    image = "registry.example.com/ai-build-tools@sha256:" + ("a" * 64)
    packages = {
        "train.yaml": make_training_pipeline(image, "", "", "http://s3.example"),
        "deploy.yaml": make_deployment_pipeline(image, "", ""),
    }
    for name, pipeline in packages.items():
        output = tmp_path / name
        compiler.Compiler().compile(pipeline, str(output))
        rendered = output.read_text()
        assert "defaultValue: kubernetes-fixture" in rendered


def test_local_path_helper_image_is_digest_pinned() -> None:
    versions = (Path(__file__).parents[2] / "versions.env").read_text()
    match = re.search(r"^LOCAL_PATH_HELPER_IMAGE=(.+)$", versions, flags=re.MULTILINE)
    assert match is not None
    assert re.fullmatch(r".+@sha256:[a-f0-9]{64}", match.group(1))


def test_runtime_health_and_prediction(tmp_path: Path) -> None:
    base = tmp_path / "base"
    adapter = tmp_path / "adapter"
    make_parent(base)
    adapter.mkdir()
    (adapter / "pytorch_lora_weights.safetensors").write_bytes(b"fixture")
    model = ModelServer("cute-bear", base, adapter)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(model))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz") as response:
            assert json.load(response)["ready"] is True
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/models/cute-bear:predict",
            data=json.dumps({"instances": [{"prompt": "cute bear", "seed": 9}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            prediction = json.load(response)
        image = base64.b64decode(prediction["predictions"][0]["image_base64"])
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        server.shutdown()
        thread.join()
