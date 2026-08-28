FROM nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ffmpeg git git-lfs libgl1 libglib2.0-0 python3.10 python3-pip python3.10-dev && \
    rm -rf /var/lib/apt/lists/* && \
    python3.10 -m pip install "pip==24.3.1" "setuptools==69.5.1" "wheel==0.45.1"
WORKDIR /workspace/MuseTalk
COPY . .
RUN python3.10 -m pip install \
    torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2 \
    --index-url https://download.pytorch.org/whl/cu118
RUN python3.10 -m pip install -r requirements.txt
RUN python3.10 -m pip install --no-build-isolation "chumpy==0.70" && \
    python3.10 -m pip install fastapi uvicorn python-multipart "huggingface_hub[hf_xet]" openmim
RUN mim install mmengine && \
    mim install "mmcv==2.0.1" && \
    mim install "mmdet==3.1.0" && \
    mim install "mmpose==1.1.0"
CMD ["python3.10", "--version"]
