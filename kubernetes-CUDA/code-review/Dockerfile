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
COPY kubernetes-CUDA/security/requirements.txt ./requirements.txt
RUN python3 -m venv --system-site-packages /opt/ai-venv \
    && /opt/ai-venv/bin/python -m pip install --no-cache-dir -r requirements.txt

# The trainer and single-adapter server are domain-neutral engines already
# proven by the retained security workflow. Code-review data and gates remain
# in this parallel directory and are not mixed with security model resources.
COPY kubernetes-CUDA/security/trainer.py ./trainer.py
COPY kubernetes-CUDA/security/generate_agent_response.py ./generate_agent_response.py
COPY kubernetes-CUDA/security/serve_adviser.py ./serve_reviewer.py
COPY kubernetes-CUDA/code-review/generate_dataset.py ./generate_dataset.py
COPY kubernetes-CUDA/code-review/evaluate_reviewer.py ./evaluate_reviewer.py
COPY kubernetes-CUDA/code-review/quality_gate.py ./quality_gate.py

RUN useradd --create-home --uid 65532 --shell /usr/sbin/nologin reviewer
USER 65532:65532

ENTRYPOINT ["python", "/opt/ai-code-review/trainer.py"]
