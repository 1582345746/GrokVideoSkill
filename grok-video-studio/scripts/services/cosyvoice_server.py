#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Iterator


def build_app(source_root: Path, model_dir: str) -> tuple[Any, Any]:
    source_root = source_root.resolve()
    sys.path.insert(0, str(source_root))
    sys.path.insert(0, str(source_root / "third_party" / "Matcha-TTS"))
    import numpy as np
    from fastapi import FastAPI, File, Form, UploadFile
    from fastapi.responses import StreamingResponse
    from cosyvoice.cli.cosyvoice import AutoModel
    from cosyvoice.utils.file_utils import load_wav

    model = AutoModel(model_dir=model_dir)
    app = FastAPI(title="Grok Video Studio CosyVoice", docs_url=None, redoc_url=None)

    def stream(output: Any) -> Iterator[bytes]:
        for item in output:
            yield (item["tts_speech"].numpy() * (2**15)).astype(np.int16).tobytes()

    def response(output: Any) -> StreamingResponse:
        return StreamingResponse(
            stream(output),
            media_type="audio/L16",
            headers={"X-Sample-Rate": str(model.sample_rate), "Cache-Control": "no-store"},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "cosyvoice",
            "sample_rate": model.sample_rate,
            "model": model_dir,
            "speakers": model.list_available_spks(),
        }

    @app.post("/inference_sft")
    async def inference_sft(tts_text: str = Form(), spk_id: str = Form()) -> StreamingResponse:
        return response(model.inference_sft(tts_text, spk_id))

    @app.post("/inference_zero_shot")
    async def inference_zero_shot(
        tts_text: str = Form(), prompt_text: str = Form(), prompt_wav: UploadFile = File()
    ) -> StreamingResponse:
        return response(model.inference_zero_shot(tts_text, prompt_text, load_wav(prompt_wav.file, 16000)))

    @app.post("/inference_instruct")
    async def inference_instruct(tts_text: str = Form(), spk_id: str = Form(), instruct_text: str = Form()) -> StreamingResponse:
        return response(model.inference_instruct(tts_text, spk_id, instruct_text))

    @app.post("/inference_instruct2")
    async def inference_instruct2(
        tts_text: str = Form(), instruct_text: str = Form(), prompt_wav: UploadFile = File()
    ) -> StreamingResponse:
        return response(model.inference_instruct2(tts_text, instruct_text, load_wav(prompt_wav.file, 16000)))

    return app, model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    args = parser.parse_args()
    if not (args.source_root / "cosyvoice").is_dir():
        raise SystemExit("CosyVoice source root is invalid")
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    app, _ = build_app(args.source_root, args.model_dir)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
