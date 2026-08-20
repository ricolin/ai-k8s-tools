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

WORKDIR /opt/ai-build-tools-security
COPY kubernetes-CUDA/security/requirements.txt ./requirements.txt
RUN python3 -m venv --system-site-packages /opt/ai-venv \
    && /opt/ai-venv/bin/python -m pip install --no-cache-dir -r requirements.txt

COPY kubernetes-CUDA/security/trainer.py ./trainer.py
COPY kubernetes-CUDA/security/generate_demo_dataset.py ./generate_demo_dataset.py
COPY kubernetes-CUDA/security/evaluate_adviser.py ./evaluate_adviser.py

RUN useradd --create-home --uid 65532 --shell /usr/sbin/nologin ai-trainer
USER 65532:65532

ENTRYPOINT ["python", "/opt/ai-build-tools-security/trainer.py"]
