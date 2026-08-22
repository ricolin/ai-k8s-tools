from __future__ import annotations

from ai_build_tools_k8s.code_review_sandbox import render_bundle


def image(character: str) -> str:
    return f"registry.example/image@sha256:{character * 64}"


def test_sandbox_separates_networked_prepare_from_offline_patch_test() -> None:
    source = {
        "id": "repo-1",
        "repository": "https://github.com/example/project",
        "commit": "a" * 40,
    }
    profile = {
        "schema_version": "1.0.0",
        "id": "python-unit",
        "fetch_image": image("a"),
        "runner_image": image("b"),
        "prepare_commands": [["python3", "-m", "venv", "/workspace/.venv"]],
        "test_commands": [["/workspace/.venv/bin/python", "-m", "pytest"]],
        "timeout_seconds": 1800,
    }
    fix = {
        "status": "PROPOSED",
        "patch_id": "fix-1",
        "unified_diff": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        "rationale": "Fix it",
        "expected_tests": ["pytest"],
    }
    bundle = render_bundle(source, profile, "review-run", "review-workspace", "local-path", fix)
    assert bundle["fetch-job.json"]["spec"]["template"]["metadata"]["labels"]["ai-k8s-tools.ricolin.dev/network-stage"] == "fetch"
    assert bundle["test-job.json"]["spec"]["template"]["metadata"]["labels"]["ai-k8s-tools.ricolin.dev/network-stage"] == "test"
    egress_values = bundle["fetch-egress.json"]["spec"]["podSelector"]["matchExpressions"][0]["values"]
    assert egress_values == ["fetch", "prepare"]
    assert "git apply --check" in bundle["test-script.json"]["data"]["run.sh"]
    assert 'if [ "${status}" -eq 0 ]; then' in bundle["test-script.json"]["data"]["run.sh"]
    assert "break" not in bundle["test-script.json"]["data"]["run.sh"]
    assert bundle["test-script.json"]["data"]["fix.patch"] == fix["unified_diff"]
    fetch = bundle["fetch-script.json"]["data"]["run.sh"]
    assert "cd /tmp" in fetch
    assert "find /workspace/source -mindepth 1" in fetch
    assert "rmdir /workspace/source" in fetch
    assert "GIT_TERMINAL_PROMPT=0 git clone" in fetch
    assert bundle["pvc.json"]["spec"]["storageClassName"] == "local-path"
    for name in ("fetch-job.json", "prepare-job.json", "test-job.json"):
        container = bundle[name]["spec"]["template"]["spec"]["containers"][0]
        assert container["imagePullPolicy"] == "IfNotPresent"
