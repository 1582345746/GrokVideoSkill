FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ffmpeg git git-lfs libsox-dev sox wget && \
    rm -rf /var/lib/apt/lists/*
RUN wget -q https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh -O /tmp/miniforge.sh && \
    bash /tmp/miniforge.sh -b -p /opt/conda && rm /tmp/miniforge.sh
ENV PATH=/opt/conda/bin:/opt/conda/envs/cosyvoice/bin:${PATH}
RUN conda create -y -n cosyvoice python=3.10 && conda install -y -n cosyvoice -c conda-forge pynini==2.1.5 && conda clean -afy
WORKDIR /workspace/CosyVoice
COPY . .
RUN /opt/conda/envs/cosyvoice/bin/pip install "pip==24.3.1" "setuptools==69.5.1" "wheel==0.45.1" \
        "numpy==1.26.4" "Cython==3.0.10" && \
    grep -vE '^(openai-whisper|pyworld)==' requirements.txt > /tmp/cosyvoice-requirements.txt && \
    /opt/conda/envs/cosyvoice/bin/pip install --no-build-isolation \
        "openai-whisper==20231117" && \
    /opt/conda/envs/cosyvoice/bin/pip install --no-build-isolation --no-deps \
        "pyworld==0.3.4" && \
    /opt/conda/envs/cosyvoice/bin/pip install -r /tmp/cosyvoice-requirements.txt && \
    /opt/conda/envs/cosyvoice/bin/pip install python-multipart
RUN /opt/conda/envs/cosyvoice/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice-ttsfrd', revision='8c0f9244a4f7622bf8017cad347ed334f0b8f735', local_dir='/workspace/CosyVoice/pretrained_models/CosyVoice-ttsfrd', allow_patterns=['resource.zip', 'ttsfrd_dependency-0.1-py3-none-any.whl', 'ttsfrd-0.4.2-cp310-cp310-linux_x86_64.whl'])" && \
    /opt/conda/envs/cosyvoice/bin/python -m zipfile -e /workspace/CosyVoice/pretrained_models/CosyVoice-ttsfrd/resource.zip /workspace/CosyVoice/pretrained_models/CosyVoice-ttsfrd && \
    /opt/conda/envs/cosyvoice/bin/pip install \
        /workspace/CosyVoice/pretrained_models/CosyVoice-ttsfrd/ttsfrd_dependency-0.1-py3-none-any.whl \
        /workspace/CosyVoice/pretrained_models/CosyVoice-ttsfrd/ttsfrd-0.4.2-cp310-cp310-linux_x86_64.whl
ENV PYTHONPATH=/workspace/CosyVoice:/workspace/CosyVoice/third_party/Matcha-TTS
CMD ["/opt/conda/envs/cosyvoice/bin/python", "--version"]
