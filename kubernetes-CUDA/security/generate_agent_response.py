from __future__ import annotations

import argparse
import hashlib
import json
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


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text())
    require(config.get("schema_version") == "1.0.0", "unsupported config schema")
    for field in ("foundation_path", "adapter_path", "prompt_path", "output_dir"):
        require(Path(config[field]).is_absolute(), f"{field} must be absolute")
    for field in ("foundation_digest", "adapter_digest"):
        require(str(config.get(field, "")).startswith("sha256:"), f"{field} is required")
    require(int(config.get("max_new_tokens", 0)) > 0, "max_new_tokens must be positive")
    require(int(config.get("max_new_tokens", 0)) <= 4096, "max_new_tokens exceeds the limit")
    return config


def load_prompt(path: Path) -> dict[str, Any]:
    prompt = json.loads(path.read_text())
    require(isinstance(prompt, dict), "prompt must be an object")
    messages = prompt.get("messages")
    require(isinstance(messages, list) and len(messages) >= 2, "prompt messages are incomplete")
    require(all(message.get("role") in {"system", "user"} for message in messages), "invalid prompt role")
    require(all(isinstance(message.get("content"), str) and message["content"] for message in messages), "empty prompt content")
    return prompt


def generate(config_path: Path) -> None:
    config = load_config(config_path)
    foundation = Path(config["foundation_path"])
    adapter = Path(config["adapter_path"])
    prompt_path = Path(config["prompt_path"])
    output = Path(config["output_dir"])
    require(foundation.is_dir(), "foundation is missing")
    require(adapter.is_dir(), "adapter is missing")
    require(prompt_path.is_file(), "prompt is missing")
    require(f"sha256:{sha256_tree(foundation)}" == config["foundation_digest"], "foundation digest mismatch")
    require(f"sha256:{sha256_tree(adapter)}" == config["adapter_digest"], "adapter digest mismatch")
    prompt = load_prompt(prompt_path)
    output.mkdir(parents=True, exist_ok=False)

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    require(torch.cuda.is_available(), "CUDA is unavailable")
    tokenizer = AutoTokenizer.from_pretrained(foundation, local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(
        foundation,
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=torch.bfloat16,
        low_cpu_mem_usage=True,
        device_map={"": 0},
    )
    model = PeftModel.from_pretrained(model, adapter, is_trainable=False)
    model.eval()
    rendered = tokenizer.apply_chat_template(
        prompt["messages"],
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    inputs = tokenizer(rendered, return_tensors="pt").to("cuda:0")
    with torch.inference_mode():
        generated = model.generate(
            **inputs,
            do_sample=False,
            max_new_tokens=int(config["max_new_tokens"]),
            pad_token_id=tokenizer.eos_token_id,
        )
    response_text = tokenizer.decode(
        generated[0, inputs["input_ids"].shape[1] :],
        skip_special_tokens=True,
    )
    response = json.loads(response_text)
    require(isinstance(response, dict), "generated response must be one JSON object")
    response_path = output / "adviser-response.json"
    response_path.write_bytes(canonical_json(response) + b"\n")
    metadata = {
        "schema_version": "1.0.0",
        "status": "CANDIDATE_AGENT_RESPONSE",
        "foundation_digest": config["foundation_digest"],
        "adapter_digest": config["adapter_digest"],
        "prompt_digest": f"sha256:{sha256_file(prompt_path)}",
        "response_digest": f"sha256:{sha256_file(response_path)}",
        "decoding": {"do_sample": False, "max_new_tokens": int(config["max_new_tokens"])},
    }
    (output / "generation-result.json").write_bytes(canonical_json(metadata) + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one typed response from an accepted adviser adapter")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    generate(Path(args.config))


if __name__ == "__main__":
    main()
