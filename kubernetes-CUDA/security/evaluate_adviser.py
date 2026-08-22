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
    require(Path(config["foundation_path"]).is_absolute(), "foundation path must be absolute")
    require(str(config["foundation_digest"]).startswith("sha256:"), "foundation digest is required")
    stages = config.get("stages")
    require(isinstance(stages, list) and [item.get("name") for item in stages] == ["foundation", "A", "B", "C"], "stages must be foundation, A, B, C")
    for item in stages:
        if item["name"] == "foundation":
            require(item.get("adapter_path") in {None, ""}, "foundation cannot have an adapter")
            require(item.get("adapter_digest") in {None, ""}, "foundation cannot have an adapter digest")
        else:
            require(Path(item["adapter_path"]).is_absolute(), "adapter path must be absolute")
            require(str(item["adapter_digest"]).startswith("sha256:"), "adapter digest is required")
    require(Path(config["prompts_path"]).is_absolute(), "prompts path must be absolute")
    require(Path(config["output_dir"]).is_absolute(), "output path must be absolute")
    require(int(config.get("max_new_tokens", 0)) > 0, "max_new_tokens must be positive")
    require(
        isinstance(config.get("normalize_redundant_contract_fields", False), bool),
        "normalize_redundant_contract_fields must be a boolean",
    )
    return config


def load_prompts(path: Path) -> list[dict[str, Any]]:
    prompts = json.loads(path.read_text())
    require(isinstance(prompts, list) and len(prompts) >= 4, "at least four prompts are required")
    ids = [item.get("id") for item in prompts]
    require(all(isinstance(value, str) and value for value in ids), "prompt IDs are required")
    require(len(ids) == len(set(ids)), "prompt IDs must be unique")
    for item in prompts:
        messages = item.get("messages")
        require(isinstance(messages, list) and len(messages) >= 2, "prompt messages are incomplete")
        require(all(message.get("role") in {"system", "user"} for message in messages), "comparison prompts cannot contain assistant responses")
    return prompts


def contract_errors(response: str) -> list[str]:
    duplicate_keys: list[str] = []

    def load_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        loaded: dict[str, Any] = {}
        for key, value in pairs:
            if key in loaded:
                duplicate_keys.append(key)
            loaded[key] = value
        return loaded

    try:
        advisory = json.loads(response, object_pairs_hook=load_object)
    except json.JSONDecodeError:
        return ["response is not one JSON object"]
    if not isinstance(advisory, dict):
        return ["response is not one JSON object"]

    expected_fields = {
        "schema_version",
        "evidence_ids",
        "proof_status",
        "observations",
        "unknowns",
        "risks",
        "remediation",
        "validation",
        "prohibited_inferences",
    }
    validation_fields = {
        "allowed_steps",
        "negative_predicate",
        "timeout_seconds",
        "stop_conditions",
        "cleanup",
    }
    errors = []
    if duplicate_keys:
        errors.append(
            "response contains duplicate JSON keys: "
            + ", ".join(sorted(set(duplicate_keys)))
        )
    if set(advisory) != expected_fields:
        errors.append("top-level fields do not match the advisory contract")
    if advisory.get("schema_version") != "1.0.0":
        errors.append("schema_version must be 1.0.0")
    validation = advisory.get("validation")
    if not isinstance(validation, dict) or set(validation) != validation_fields:
        errors.append("validation fields do not match the advisory contract")
    return errors


def normalize_advisory(response: str) -> tuple[str, list[str]]:
    normalizable_validation_fields = {"prohibited_inferences"}
    duplicate_keys: list[str] = []

    def load_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        loaded: dict[str, Any] = {}
        for key, value in pairs:
            if key in loaded:
                require(loaded[key] == value, f"conflicting duplicate JSON key: {key}")
                duplicate_keys.append(key)
            loaded[key] = value
        return loaded

    advisory = json.loads(response, object_pairs_hook=load_object)
    require(isinstance(advisory, dict), "response is not one JSON object")
    validation = advisory.get("validation")
    require(isinstance(validation, dict), "validation must be an object")
    actions = [f"collapsed-identical-duplicate:{key}" for key in sorted(set(duplicate_keys))]
    extra_validation = set(validation) - {
        "allowed_steps",
        "negative_predicate",
        "timeout_seconds",
        "stop_conditions",
        "cleanup",
    }
    for key in sorted(extra_validation):
        require(
            key in normalizable_validation_fields
            and key in advisory
            and advisory[key] == validation[key],
            f"cannot normalize non-redundant validation field: {key}",
        )
        del validation[key]
        actions.append(f"removed-redundant-validation-field:{key}")
    normalized = canonical_json(advisory).decode()
    require(not contract_errors(normalized), "normalized advisory still violates the contract")
    return normalized, actions


def evaluate(config_path: Path) -> None:
    config = load_config(config_path)
    foundation = Path(config["foundation_path"])
    prompts_path = Path(config["prompts_path"])
    output = Path(config["output_dir"])
    require(foundation.is_dir(), "foundation is missing")
    require(f"sha256:{sha256_tree(foundation)}" == config["foundation_digest"], "foundation digest mismatch")
    prompts = load_prompts(prompts_path)
    output.mkdir(parents=True, exist_ok=False)

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    require(torch.cuda.is_available(), "CUDA is unavailable")
    tokenizer = AutoTokenizer.from_pretrained(foundation, local_files_only=True, trust_remote_code=False)
    records: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
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
            inputs = tokenizer(rendered, return_tensors="pt").to("cuda:0")
            with torch.inference_mode():
                generated = model.generate(
                    **inputs,
                    do_sample=False,
                    max_new_tokens=int(config["max_new_tokens"]),
                    pad_token_id=tokenizer.eos_token_id,
                )
            response = tokenizer.decode(
                generated[0, inputs["input_ids"].shape[1] :],
                skip_special_tokens=True,
            )
            errors = contract_errors(response)
            attempts.append(
                {
                    "schema_version": "1.0.0",
                    "stage": stage["name"],
                    "prompt_id": prompt["id"],
                    "attempt": 1,
                    "contract_errors": errors,
                    "response": response,
                }
            )
            normalization_actions: list[str] = []
            if errors and bool(config.get("normalize_redundant_contract_fields", False)):
                try:
                    normalized, normalization_actions = normalize_advisory(response)
                except (json.JSONDecodeError, ValueError):
                    pass
                else:
                    attempts.append(
                        {
                            "schema_version": "1.0.0",
                            "stage": stage["name"],
                            "prompt_id": prompt["id"],
                            "attempt": 1,
                            "operation": "canonical-normalization",
                            "contract_errors": [],
                            "normalization_actions": normalization_actions,
                            "response": normalized,
                        }
                    )
                    response = normalized
                    errors = []
            records.append(
                {
                    "schema_version": "1.0.0",
                    "stage": stage["name"],
                    "prompt_id": prompt["id"],
                    "foundation_digest": config["foundation_digest"],
                    "adapter_digest": stage.get("adapter_digest"),
                    "prompt_digest": f"sha256:{hashlib.sha256(canonical_json(prompt)).hexdigest()}",
                    "decoding": {
                        "do_sample": False,
                        "max_new_tokens": int(config["max_new_tokens"]),
                    },
                    "attempt_count": 1,
                    "final_contract_errors": errors,
                    "normalization_actions": normalization_actions,
                    "response": response,
                }
            )
        del model
        torch.cuda.empty_cache()

    responses = output / "responses.jsonl"
    responses.write_bytes(b"".join(canonical_json(record) + b"\n" for record in records))
    attempts_file = output / "response-attempts.jsonl"
    attempts_file.write_bytes(b"".join(canonical_json(record) + b"\n" for record in attempts))
    metadata = {
        "schema_version": "1.0.0",
        "status": "CANDIDATE_COMPARISON",
        "foundation_digest": config["foundation_digest"],
        "prompts_digest": f"sha256:{sha256_file(prompts_path)}",
        "response_count": len(records),
        "responses_digest": f"sha256:{sha256_file(responses)}",
        "response_attempts_digest": f"sha256:{sha256_file(attempts_file)}",
        "stages": config["stages"],
    }
    (output / "comparison-result.json").write_bytes(canonical_json(metadata) + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline deterministic foundation/A/B/C adviser comparison")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    evaluate(Path(args.config))


if __name__ == "__main__":
    main()
