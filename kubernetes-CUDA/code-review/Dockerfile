ARG PYTORCH_CUDA_IMAGE
FROM ${PYTORCH_CUDA_IMAGE}

ARG SOURCE_DATE_EPOCH=0

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/ai-venv/bin:${PATH}

WORKDIR /opt/ai-code-review
COPY kubernetes-CUDA/common/requirements.txt ./requirements.txt
RUN python3 -m venv --system-site-packages /opt/ai-venv \
    && /opt/ai-venv/bin/python -m pip install --no-cache-dir -r requirements.txt

# Keep the reusable model runtime independent from workflow-specific datasets
# and evaluation gates.
COPY kubernetes-CUDA/common/text_adapter_trainer.py ./trainer.py
COPY kubernetes-CUDA/common/serve_text_adapter.py ./serve_reviewer.py
COPY kubernetes-CUDA/code-review/generate_dataset.py ./generate_dataset.py
COPY kubernetes-CUDA/code-review/evaluate_reviewer.py ./evaluate_reviewer.py
COPY kubernetes-CUDA/code-review/quality_gate.py ./quality_gate.py
COPY kubernetes-CUDA/code-review/capability_gate.py ./capability_gate.py
COPY kubernetes/workflows/src/ai_build_tools_k8s/__init__.py ./ai_build_tools_k8s/__init__.py
COPY kubernetes/workflows/src/ai_build_tools_k8s/workflow.py ./ai_build_tools_k8s/workflow.py
COPY kubernetes/workflows/src/ai_build_tools_k8s/code_review_model.py ./ai_build_tools_k8s/code_review_model.py

RUN useradd --create-home --uid 65532 --shell /usr/sbin/nologin reviewer
USER 65532:65532

ENTRYPOINT ["python", "/opt/ai-code-review/trainer.py"]
