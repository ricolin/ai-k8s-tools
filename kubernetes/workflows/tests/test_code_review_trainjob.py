from __future__ import annotations

from ai_build_tools_k8s.code_review_trainjob import terminal_state


def test_trainjob_terminal_state() -> None:
    assert terminal_state({"status": {"conditions": []}}) is None
    assert terminal_state(
        {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
    ) == "COMPLETE"
    assert terminal_state(
        {"status": {"conditions": [{"type": "Failed", "status": "True"}]}}
    ) == "FAILED"
