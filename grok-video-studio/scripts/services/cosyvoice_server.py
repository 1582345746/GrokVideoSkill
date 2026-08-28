#!/usr/bin/env python3

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator


MAX_PROMPT_BYTES = 64 * 1024 * 1024


def _store_prompt(upload: Any) -> Path:
    handle = tempfile.NamedTemporaryFile(prefix="gvs-cosyvoice-", suffix=".wav", delete=False)
    path = Path(handle.name)
    total = 0
    try:
        with handle:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PROMPT_BYTES:
                    raise ValueError("voice reference exceeds 64 MB")
                handle.write(chunk)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _cosyvoice3_prompt(text: str, *, instruction: bool = False) -> str:
    value = text.strip()
    if "<|endofprompt|>" in value:
        return value
    if instruction:
        return f"You are a helpful assistant. {value}<|endofprompt|>"
    return f"You are a helpful assistant.<|endofprompt|>{value}"


def build_app(source_root: Path, model_dir: str) -> tuple[Any, Any]:
    source_root = source_root.resolve()
    sys.path.insert(0, str(source_root))
    sys.path.insert(0, str(source_root / "third_party" / "Matcha-TTS"))
    import numpy as np
    from fastapi import FastAPI, File, Form, UploadFile
    from fastapi.responses import StreamingResponse
    from cosyvoice.cli.cosyvoice import AutoModel

    model = AutoModel(model_dir=model_dir)
    is_cosyvoice3 = model.__class__.__name__ == "CosyVoice3"
    app = FastAPI(title="Grok Video Studio CosyVoice", docs_url=None, redoc_url=None)

    def stream(output: Any, cleanup: Path | None = None) -> Iterator[bytes]:
        try:
            for item in output:
                yield (item["tts_speech"].numpy() * (2**15)).astype(np.int16).tobytes()
        finally:
            if cleanup is not None:
                cleanup.unlink(missing_ok=True)

    def response(output: Any, cleanup: Path | None = None) -> StreamingResponse:
        return StreamingResponse(
            stream(output, cleanup),
            media_type="audio/L16",
            headers={"X-Sample-Rate": str(model.sample_rate), "Cache-Control": "no-store"},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        frontend = str(getattr(model.frontend, "text_frontend", ""))
        return {
            "ok": bool(frontend),
            "service": "cosyvoice",
            "sample_rate": model.sample_rate,
            "model": model_dir,
            "model_type": model.__class__.__name__,
            "text_frontend": frontend or "unavailable",
            "speakers": model.list_available_spks(),
        }

    @app.post("/inference_sft")
    async def inference_sft(tts_text: str = Form(), spk_id: str = Form()) -> StreamingResponse:
        return response(model.inference_sft(tts_text, spk_id))

    @app.post("/inference_zero_shot")
    async def inference_zero_shot(
        tts_text: str = Form(), prompt_text: str = Form(), prompt_wav: UploadFile = File()
    ) -> StreamingResponse:
        prompt_path = _store_prompt(prompt_wav)
        normalized_prompt = _cosyvoice3_prompt(prompt_text) if is_cosyvoice3 else prompt_text
        return response(model.inference_zero_shot(tts_text, normalized_prompt, str(prompt_path)), prompt_path)

    @app.post("/inference_instruct")
    async def inference_instruct(tts_text: str = Form(), spk_id: str = Form(), instruct_text: str = Form()) -> StreamingResponse:
        return response(model.inference_instruct(tts_text, spk_id, instruct_text))

    @app.post("/inference_instruct2")
    async def inference_instruct2(
        tts_text: str = Form(), instruct_text: str = Form(), prompt_wav: UploadFile = File()
    ) -> StreamingResponse:
        prompt_path = _store_prompt(prompt_wav)
        normalized_instruction = _cosyvoice3_prompt(instruct_text, instruction=True) if is_cosyvoice3 else instruct_text
        return response(model.inference_instruct2(tts_text, normalized_instruction, str(prompt_path)), prompt_path)

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
