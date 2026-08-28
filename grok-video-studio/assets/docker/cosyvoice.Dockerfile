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
RUN /opt/conda/envs/cosyvoice/bin/pip install -r requirements.txt && \
    /opt/conda/envs/cosyvoice/bin/pip install python-multipart
ENV PYTHONPATH=/workspace/CosyVoice:/workspace/CosyVoice/third_party/Matcha-TTS
CMD ["/opt/conda/envs/cosyvoice/bin/python", "--version"]
