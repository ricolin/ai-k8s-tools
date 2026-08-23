from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from quality_gate import normalize_response_text


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_request(payload: dict[str, Any], model_name: str) -> list[dict[str, str]]:
    require(payload.get("model") == model_name, "model identity mismatch")
    require(float(payload.get("temperature", 0)) == 0, "only deterministic decoding is supported")
    response_format = payload.get("response_format", {})
    require(response_format == {"type": "json_object"}, "JSON object response format is required")
    messages = payload.get("messages")
    require(isinstance(messages, list) and len(messages) >= 2, "messages are incomplete")
    normalized: list[dict[str, str]] = []
    for message in messages:
        require(message.get("role") in {"system", "user"}, "unsupported message role")
        require(isinstance(message.get("content"), str) and message["content"], "empty message content")
        normalized.append({"role": message["role"], "content": message["content"]})
    return normalized


def openai_response(model_name: str, content: str, prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{hashlib.sha256(content.encode()).hexdigest()[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class TextAdapterServer:
    def __init__(
        self,
        foundation: Path,
        adapter: Path,
        foundation_digest: str,
        adapter_digest: str,
        model_name: str,
        max_new_tokens: int,
        response_prefix: str = "",
    ) -> None:
        require(foundation.is_dir(), "foundation is missing")
        require(adapter.is_dir(), "adapter is missing")
        require(f"sha256:{sha256_tree(foundation)}" == foundation_digest, "foundation digest mismatch")
        require(f"sha256:{sha256_tree(adapter)}" == adapter_digest, "adapter digest mismatch")
        require(1 <= max_new_tokens <= 4096, "max_new_tokens is invalid")
        require(response_prefix in {"", "{"}, "unsupported response prefix")

        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        require(torch.cuda.is_available(), "CUDA is unavailable")
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(
            foundation,
            local_files_only=True,
            trust_remote_code=False,
        )
        model = AutoModelForCausalLM.from_pretrained(
            foundation,
            local_files_only=True,
            trust_remote_code=False,
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            device_map={"": 0},
        )
        self.model = PeftModel.from_pretrained(model, adapter, is_trainable=False)
        self.model.eval()
        self.foundation_digest = foundation_digest
        self.adapter_digest = adapter_digest
        self.model_name = model_name
        self.max_new_tokens = max_new_tokens
        self.response_prefix = response_prefix
        self.lock = threading.Lock()

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = validate_request(payload, self.model_name)
        rendered = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        rendered += self.response_prefix
        inputs = self.tokenizer(rendered, return_tensors="pt").to("cuda:0")
        with self.lock, self.torch.inference_mode():
            generated = self.model.generate(
                **inputs,
                do_sample=False,
                max_new_tokens=self.max_new_tokens,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        generated_tokens = generated[0, inputs["input_ids"].shape[1] :]
        content = self.response_prefix + self.tokenizer.decode(generated_tokens, skip_special_tokens=True)
        content, _ = normalize_response_text(content)
        parsed = json.loads(content, strict=False)
        require(isinstance(parsed, dict), "generated response must be one JSON object")
        compact = canonical_json(parsed).decode()
        return openai_response(
            self.model_name,
            compact,
            int(inputs["input_ids"].shape[1]),
            int(generated_tokens.shape[0]),
        )


def handler_factory(server: TextAdapterServer) -> type[BaseHTTPRequestHandler]:
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
                self.write_json(
                    200,
                    {
                        "ready": True,
                        "model": server.model_name,
                        "foundation_digest": server.foundation_digest,
                        "adapter_digest": server.adapter_digest,
                    },
                )
                return
            if self.path == "/v1/models":
                self.write_json(
                    200,
                    {"object": "list", "data": [{"id": server.model_name, "object": "model"}]},
                )
                return
            self.write_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/chat/completions":
                self.write_json(404, {"error": "not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= 1024 * 1024, "request body size is invalid")
                payload = json.loads(self.rfile.read(size))
                require(isinstance(payload, dict), "request body must be an object")
                self.write_json(200, server.generate(payload))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                self.write_json(400, {"error": str(error)})

        def log_message(self, format: str, *args: object) -> None:
            print(format % args, flush=True)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI-compatible released text-adapter server")
    parser.add_argument("--foundation", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--foundation-digest", required=True)
    parser.add_argument("--adapter-digest", required=True)
    parser.add_argument("--model-name", default="text-adapter-c")
    parser.add_argument("--max-new-tokens", type=int, default=1536)
    parser.add_argument("--response-prefix", default="")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = TextAdapterServer(
        args.foundation,
        args.adapter,
        args.foundation_digest,
        args.adapter_digest,
        args.model_name,
        args.max_new_tokens,
        args.response_prefix,
    )
    ThreadingHTTPServer(("0.0.0.0", args.port), handler_factory(server)).serve_forever()


if __name__ == "__main__":
    main()
