from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from quality_gate import normalize_response_text, validate_response_text


STAGE_ORDER = ("foundation", "A", "B", "C")


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


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text())
    require(config.get("schema_version") == "1.0.0", "unsupported config schema")
    require(Path(config["foundation_path"]).is_absolute(), "foundation path must be absolute")
    require(str(config["foundation_digest"]).startswith("sha256:"), "foundation digest is required")
    stages = config.get("stages")
    names = [item.get("name") for item in stages] if isinstance(stages, list) else []
    require(
        bool(names)
        and len(names) == len(set(names))
        and all(name in STAGE_ORDER for name in names)
        and names == sorted(names, key=STAGE_ORDER.index),
        "stages must be a non-empty ordered subset of foundation, A, B, C",
    )
    for item in stages:
        if item["name"] == "foundation":
            require(item.get("adapter_path") in {None, ""}, "foundation cannot have an adapter")
        else:
            require(Path(item["adapter_path"]).is_absolute(), "adapter path must be absolute")
            require(str(item["adapter_digest"]).startswith("sha256:"), "adapter digest is required")
    require(Path(config["prompts_path"]).is_absolute(), "prompts path must be absolute")
    require(Path(config["output_dir"]).is_absolute(), "output path must be absolute")
    require(1 <= int(config.get("max_new_tokens", 0)) <= 4096, "max_new_tokens is invalid")
    require(config.get("response_prefix", "") in {"", "{"}, "unsupported response prefix")
    return config


def evaluate(config_path: Path) -> None:
    config = load_config(config_path)
    foundation = Path(config["foundation_path"])
    prompts_path = Path(config["prompts_path"])
    output = Path(config["output_dir"])
    require(foundation.is_dir(), "foundation is missing")
    require(f"sha256:{sha256_tree(foundation)}" == config["foundation_digest"], "foundation digest mismatch")
    prompts = json.loads(prompts_path.read_text())
    require(isinstance(prompts, list) and len(prompts) >= 6, "comparison prompts are incomplete")
    output.mkdir(parents=True, exist_ok=False)
    response_prefix = str(config.get("response_prefix", ""))

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    require(torch.cuda.is_available(), "CUDA is unavailable")
    tokenizer = AutoTokenizer.from_pretrained(foundation, local_files_only=True, trust_remote_code=False)
    records = []
    for stage in config["stages"]:
        adapter = Path(stage["adapter_path"]) if stage.get("adapter_path") else None
        if adapter:
            require(adapter.is_dir(), f"adapter is missing: {stage['name']}")
            require(f"sha256:{sha256_tree(adapter)}" == stage["adapter_digest"], f"adapter digest mismatch: {stage['name']}")
        model = AutoModelForCausalLM.from_pretrained(
            foundation,
            local_files_only=True,
            trust_remote_code=False,
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            device_map={"": 0},
        )
        if adapter:
            model = PeftModel.from_pretrained(model, adapter, is_trainable=False)
        model.eval()
        for prompt in prompts:
            rendered = tokenizer.apply_chat_template(
                prompt["messages"],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            rendered += response_prefix
            inputs = tokenizer(rendered, return_tensors="pt").to("cuda:0")
            with torch.inference_mode():
                generated = model.generate(
                    **inputs,
                    do_sample=False,
                    max_new_tokens=int(config["max_new_tokens"]),
                    pad_token_id=tokenizer.eos_token_id,
                )
            response = response_prefix + tokenizer.decode(
                generated[0, inputs["input_ids"].shape[1] :],
                skip_special_tokens=True,
            )
            raw_response = response
            response, normalizations = normalize_response_text(response)
            parsed, errors = validate_response_text(response)
            if parsed is not None:
                response = canonical_json(parsed).decode()
            record = {
                "schema_version": "1.0.0",
                "stage": stage["name"],
                "prompt_id": prompt["id"],
                "expected_reviewer_identity": prompt["expected_reviewer_identity"],
                "foundation_digest": config["foundation_digest"],
                "adapter_digest": stage.get("adapter_digest"),
                "prompt_digest": f"sha256:{hashlib.sha256(canonical_json(prompt)).hexdigest()}",
                "decoding": {"do_sample": False, "max_new_tokens": int(config["max_new_tokens"])},
                "contract_errors": errors,
                "response": response,
                "response_normalizations": list(normalizations),
            }
            if "expected_finding" in prompt:
                record["expected_finding"] = prompt["expected_finding"]
            if normalizations:
                record["raw_response_sha256"] = (
                    f"sha256:{hashlib.sha256(raw_response.encode()).hexdigest()}"
                )
            records.append(record)
        del model
        torch.cuda.empty_cache()
    responses = output / "responses.jsonl"
    responses.write_bytes(b"".join(canonical_json(record) + b"\n" for record in records))
    (output / "comparison-result.json").write_bytes(
        canonical_json(
            {
                "schema_version": "1.0.0",
                "status": "CANDIDATE_COMPARISON",
                "foundation_digest": config["foundation_digest"],
                "prompts_digest": f"sha256:{sha256_file(prompts_path)}",
                "responses_digest": f"sha256:{sha256_file(responses)}",
                "response_count": len(records),
                "stages": config["stages"],
            }
        )
        + b"\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic code-review comparison for an ordered stage subset"
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    evaluate(Path(args.config))


if __name__ == "__main__":
    main()
