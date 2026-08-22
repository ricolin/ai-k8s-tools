ARG UBUNTU_IMAGE
FROM ${UBUNTU_IMAGE}

ARG SOURCE_DATE_EPOCH=0
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       bash \
       ca-certificates \
       cargo \
       git \
       golang-go \
       python3 \
       python3-pip \
       python3-venv \
       rustc \
       shellcheck \
       yamllint \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 65532 --shell /usr/sbin/nologin sandbox
USER 65532:65532
WORKDIR /workspace/source

CMD ["bash", "-lc", "python3 --version && go version && rustc --version && shellcheck --version && yamllint --version"]
