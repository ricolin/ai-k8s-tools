ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/ai-build-tools-kubernetes/src

WORKDIR /opt/ai-build-tools-kubernetes
COPY kubernetes/workflows/src ./src

USER 65532:65532
ENTRYPOINT ["python", "-m", "ai_build_tools_k8s.server"]
