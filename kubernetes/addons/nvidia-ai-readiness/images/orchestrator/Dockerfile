ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG KUBECTL_VERSION
ARG KUBECTL_SHA256

RUN test -n "${KUBECTL_VERSION}" \
    && test -n "${KUBECTL_SHA256}" \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
         ca-certificates curl jq \
    && curl -fL --retry 5 \
         -o /usr/local/bin/kubectl \
         "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && printf '%s  %s\n' "${KUBECTL_SHA256}" /usr/local/bin/kubectl \
         | sha256sum --check \
    && chmod 0755 /usr/local/bin/kubectl \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

COPY kubernetes/addons/nvidia-ai-readiness/images/orchestrator/orchestrate.sh \
  /usr/local/bin/nvidia-ai-readiness
RUN chmod 0755 /usr/local/bin/nvidia-ai-readiness \
    && mkdir -p /home/nonroot \
    && chown 65532:65532 /home/nonroot

USER 65532:65532
ENV HOME=/home/nonroot
ENTRYPOINT ["/usr/local/bin/nvidia-ai-readiness"]
