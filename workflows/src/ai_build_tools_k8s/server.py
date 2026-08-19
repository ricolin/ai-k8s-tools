from __future__ import annotations

import argparse
import base64
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .workflow import canonical_json, sha256_tree, write_deterministic_png


class ModelServer:
    def __init__(self, model_name: str, base_dir: Path, adapter_dir: Path) -> None:
        if not base_dir.exists():
            raise FileNotFoundError(f"base model directory is missing: {base_dir}")
        if not adapter_dir.exists():
            raise FileNotFoundError(f"adapter directory is missing: {adapter_dir}")
        self.model_name = model_name
        self.base_digest = sha256_tree(base_dir)
        self.adapter_digest = sha256_tree(adapter_dir)
        self.model_digest = hashlib.sha256(
            canonical_json({"base": self.base_digest, "adapter": self.adapter_digest})
        ).hexdigest()

    def predict(self, payload: dict[str, Any]) -> dict[str, Any]:
        instances = payload.get("instances")
        if not isinstance(instances, list) or not instances:
            raise ValueError("instances must be a non-empty list")
        predictions = []
        for instance in instances:
            prompt = str(instance["prompt"])
            seed = int(instance.get("seed", 0))
            temporary = Path("/tmp") / f"prediction-{hashlib.sha256(canonical_json(instance)).hexdigest()}.png"
            write_deterministic_png(
                temporary,
                canonical_json(
                    {
                        "model_digest": self.model_digest,
                        "prompt": prompt,
                        "seed": seed,
                    }
                ),
            )
            predictions.append(
                {
                    "prompt": prompt,
                    "seed": seed,
                    "image_base64": base64.b64encode(temporary.read_bytes()).decode(),
                }
            )
            temporary.unlink(missing_ok=True)
        return {
            "model_name": self.model_name,
            "model_digest": f"sha256:{self.model_digest}",
            "predictions": predictions,
        }


def handler_factory(model: ModelServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, body: dict[str, Any]) -> None:
            payload = canonical_json(body) + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/healthz", "/readyz"}:
                self._json(
                    200,
                    {
                        "ready": True,
                        "model_name": model.model_name,
                        "model_digest": f"sha256:{model.model_digest}",
                    },
                )
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != f"/v1/models/{model.model_name}:predict":
                self._json(404, {"error": "not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size))
                self._json(200, model.predict(payload))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                self._json(400, {"error": str(error)})

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--adapter-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    model = ModelServer(args.model_name, args.base_dir, args.adapter_dir)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler_factory(model)).serve_forever()


if __name__ == "__main__":
    main()
