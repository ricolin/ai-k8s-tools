ARG PYTORCH_CUDA_IMAGE
FROM ${PYTORCH_CUDA_IMAGE}

ARG DIFFUSERS_COMMIT
ARG SOURCE_DATE_EPOCH=0

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    DIFFUSERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/ai-venv \
    PATH=/opt/ai-venv/bin:$PATH

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ai-build-tools-image
RUN python3 -m venv --system-site-packages "${VIRTUAL_ENV}"
COPY kubernetes-CUDA/image/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN test -n "${DIFFUSERS_COMMIT}" \
    && rm -rf /opt/diffusers \
    && git clone https://github.com/huggingface/diffusers.git /opt/diffusers \
    && git -C /opt/diffusers checkout --detach "${DIFFUSERS_COMMIT}" \
    && test "$(git -C /opt/diffusers rev-parse HEAD)" = "${DIFFUSERS_COMMIT}" \
    && python -m pip install --no-cache-dir --no-deps /opt/diffusers

RUN script=/opt/diffusers/examples/advanced_diffusion_training/train_dreambooth_lora_sdxl_advanced.py \
    && grep -Fq 'pipeline.load_lora_weights(args.output_dir)' "${script}" \
    && sed -i \
      's/pipeline.load_lora_weights(args.output_dir)$/pipeline.load_lora_weights(args.output_dir, weight_name="pytorch_lora_weights.safetensors")/' \
      "${script}"

COPY kubernetes-CUDA/image/prepare_dataset.py ./prepare_dataset.py
COPY kubernetes-CUDA/image/generate_demo_dataset.py ./generate_demo_dataset.py
COPY kubernetes-CUDA/image/compose_parent.py ./compose_parent.py
COPY kubernetes-CUDA/image/train_stage.py ./train_stage.py
COPY kubernetes-CUDA/image/generate_comparison.py ./generate_comparison.py

RUN useradd --create-home --uid 65532 --shell /usr/sbin/nologin ai-trainer
USER 65532:65532

ENTRYPOINT ["python", "/opt/ai-build-tools-image/train_stage.py"]
