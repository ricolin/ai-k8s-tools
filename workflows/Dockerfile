ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ARG KUBECTL_IMAGE=registry.k8s.io/kubectl:v1.34.3@sha256:1c6309bd167feb49b2bc1605bce05973002872fd0d26a2e76499ed0c903ea3cf
FROM ${KUBECTL_IMAGE} AS kubectl
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/ai-build-tools-kubernetes/.venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=kubectl /bin/kubectl /usr/local/bin/kubectl
WORKDIR /opt/ai-build-tools-kubernetes
COPY kubernetes/workflows/pyproject.toml ./pyproject.toml
COPY kubernetes/workflows/uv.lock ./uv.lock
RUN python -m pip install --no-cache-dir uv==0.10.9 \
    && uv sync --frozen --no-dev --no-install-project
COPY kubernetes/workflows/src ./src
RUN uv sync --frozen --no-dev --no-editable

USER 65532:65532
ENTRYPOINT ["ai-k8s-workflow"]
