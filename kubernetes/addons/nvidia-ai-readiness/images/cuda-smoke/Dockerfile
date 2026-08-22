ARG CUDA_DEVEL_IMAGE
ARG CUDA_RUNTIME_IMAGE
FROM ${CUDA_DEVEL_IMAGE} AS build
COPY kubernetes/addons/nvidia-ai-readiness/images/cuda-smoke/cuda-smoke.cu /src/cuda-smoke.cu
RUN nvcc -O2 -o /cuda-smoke /src/cuda-smoke.cu

FROM ${CUDA_RUNTIME_IMAGE}
COPY --from=build /cuda-smoke /usr/local/bin/cuda-smoke
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/cuda-smoke"]
