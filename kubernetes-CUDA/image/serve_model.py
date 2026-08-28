from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and ".cache" not in candidate.relative_to(path).parts
    ):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def parse_adapter(values: list[str]) -> dict[str, Any]:
    name, raw_path, expected_digest, raw_scale = values
    path = Path(raw_path)
    require(name.replace("-", "").isalnum(), "adapter name is invalid")
    require(path.is_dir(), f"adapter directory is missing: {path}")
    weights = path / "pytorch_lora_weights.safetensors"
    require(weights.is_file(), f"adapter weights are missing: {weights}")
    observed_digest = f"sha256:{sha256_file(weights)}"
    require(observed_digest == expected_digest, f"adapter digest mismatch: {name}")
    scale = float(raw_scale)
    require(0 < scale <= 2, f"adapter scale is invalid: {name}")
    return {
        "name": name,
        "path": path,
        "weights": weights.name,
        "digest": observed_digest,
        "scale": scale,
    }


def validate_instance(instance: dict[str, Any]) -> dict[str, Any]:
    require(isinstance(instance.get("prompt"), str) and instance["prompt"], "prompt is required")
    negative_prompt = instance.get("negative_prompt", "")
    require(isinstance(negative_prompt, str), "negative_prompt must be text")
    width = int(instance.get("width", 1024))
    height = int(instance.get("height", 1024))
    steps = int(instance.get("steps", 30))
    guidance = float(instance.get("guidance", 5.5))
    seed = int(instance.get("seed", 0))
    require(width in {512, 768, 1024}, "width must be 512, 768, or 1024")
    require(height in {512, 768, 1024}, "height must be 512, 768, or 1024")
    require(1 <= steps <= 50, "steps must be between 1 and 50")
    require(0 <= guidance <= 20, "guidance must be between 0 and 20")
    require(0 <= seed < 2**63, "seed is out of range")
    return {
        "prompt": instance["prompt"],
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "guidance": guidance,
        "seed": seed,
    }


class ImageAdapterServer:
    def __init__(
        self,
        foundation: Path,
        foundation_digest: str,
        model_name: str,
        adapters: list[list[str]],
    ) -> None:
        require(foundation.is_dir(), "foundation is missing")
        require(
            f"sha256:{sha256_tree(foundation)}" == foundation_digest,
            "foundation digest mismatch",
        )
        require(model_name, "model name is required")
        require(len(adapters) in {1, 2}, "one or two adapters are required")
        self.adapters = [parse_adapter(value) for value in adapters]
        names = [adapter["name"] for adapter in self.adapters]
        require(len(names) == len(set(names)), "adapter names must be unique")

        os.environ.update(
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            DIFFUSERS_OFFLINE="1",
            TOKENIZERS_PARALLELISM="false",
        )
        import torch
        from diffusers import StableDiffusionXLPipeline

        require(torch.cuda.is_available(), "CUDA is unavailable")
        pipeline = StableDiffusionXLPipeline.from_pretrained(
            foundation,
            local_files_only=True,
            torch_dtype=torch.bfloat16,
            variant="fp16",
        ).to("cuda")
        for adapter in self.adapters:
            pipeline.load_lora_weights(
                adapter["path"],
                weight_name=adapter["weights"],
                adapter_name=adapter["name"],
                local_files_only=True,
            )
        pipeline.set_adapters(
            names,
            adapter_weights=[adapter["scale"] for adapter in self.adapters],
        )
        pipeline.set_progress_bar_config(disable=True)

        self.foundation_digest = foundation_digest
        self.model_name = model_name
        self.pipeline = pipeline
        self.torch = torch
        self.lock = threading.Lock()

    def identity(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "foundation_digest": self.foundation_digest,
            "adapters": [
                {
                    "name": adapter["name"],
                    "digest": adapter["digest"],
                    "scale": adapter["scale"],
                }
                for adapter in self.adapters
            ],
        }

    def predict(self, payload: dict[str, Any]) -> dict[str, Any]:
        instances = payload.get("instances")
        require(isinstance(instances, list) and len(instances) == 1, "exactly one instance is required")
        instance = validate_instance(instances[0])
        generator = self.torch.Generator(device="cuda").manual_seed(instance["seed"])
        with self.lock, self.torch.inference_mode():
            image = self.pipeline(
                prompt=instance["prompt"],
                negative_prompt=instance["negative_prompt"],
                width=instance["width"],
                height=instance["height"],
                num_inference_steps=instance["steps"],
                guidance_scale=instance["guidance"],
                generator=generator,
            ).images[0]
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        encoded = stream.getvalue()
        return {
            **self.identity(),
            "predictions": [
                {
                    **instance,
                    "sha256": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
                    "image_base64": base64.b64encode(encoded).decode(),
                }
            ],
        }


def handler_factory(server: ImageAdapterServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def write_json(self, status: int, value: dict[str, Any]) -> None:
            body = canonical_json(value) + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/health", "/healthz", "/readyz"}:
                self.write_json(200, {"ready": True, **server.identity()})
                return
            if self.path == "/v1/models":
                self.write_json(200, {"object": "list", "data": [{"id": server.model_name}]})
                return
            self.write_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != f"/v1/models/{server.model_name}:predict":
                self.write_json(404, {"error": "not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= 1024 * 1024, "request body size is invalid")
                payload = json.loads(self.rfile.read(size))
                require(isinstance(payload, dict), "request body must be an object")
                self.write_json(200, server.predict(payload))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                self.write_json(400, {"error": str(error)})

        def log_message(self, format: str, *args: object) -> None:
            print(format % args, flush=True)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Digest-bound SDXL LoRA serving runtime")
    parser.add_argument("--foundation", type=Path, required=True)
    parser.add_argument("--foundation-digest", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument(
        "--adapter",
        action="append",
        nargs=4,
        metavar=("NAME", "PATH", "SHA256", "SCALE"),
        required=True,
    )
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ImageAdapterServer(
        args.foundation,
        args.foundation_digest,
        args.model_name,
        args.adapter,
    )
    ThreadingHTTPServer(("0.0.0.0", args.port), handler_factory(server)).serve_forever()


if __name__ == "__main__":
    main()
