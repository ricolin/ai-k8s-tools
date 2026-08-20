from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "kubernetes-CUDA/security/serve_adviser.py"
SPEC = importlib.util.spec_from_file_location("serve_adviser", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def request() -> dict:
    return {
        "model": "security-adviser-c",
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Assess synthetic evidence."},
        ],
    }


def test_request_requires_exact_model_and_deterministic_json() -> None:
    assert MODULE.validate_request(request(), "security-adviser-c") == request()["messages"]
    value = request()
    value["temperature"] = 0.2
    with pytest.raises(ValueError, match="deterministic"):
        MODULE.validate_request(value, "security-adviser-c")
    value = request()
    value["model"] = "other"
    with pytest.raises(ValueError, match="identity"):
        MODULE.validate_request(value, "security-adviser-c")


def test_openai_response_preserves_model_output_and_usage() -> None:
    response = MODULE.openai_response("security-adviser-c", '{"status":"ok"}', 10, 4)
    assert response["model"] == "security-adviser-c"
    assert response["choices"][0]["message"]["content"] == '{"status":"ok"}'
    assert response["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
