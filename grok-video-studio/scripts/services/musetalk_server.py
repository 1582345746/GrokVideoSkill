#!/usr/bin/env python3

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def _copy_limited(source: Any, destination: Path) -> None:
    total = 0
    with destination.open("wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("upload exceeds 512 MB")
            output.write(chunk)


def build_app(source_root: Path, models_root: Path, python: str, timeout: int) -> Any:
    from fastapi import FastAPI, File, HTTPException, UploadFile
    from fastapi.responses import Response

    source_root = source_root.resolve()
    models_root = models_root.resolve()
    lock = threading.Lock()
    app = FastAPI(title="Grok Video Studio MuseTalk", docs_url=None, redoc_url=None)

    required = [
        models_root / "musetalkV15" / "unet.pth",
        models_root / "musetalkV15" / "musetalk.json",
        models_root / "whisper" / "config.json",
        models_root / "whisper" / "pytorch_model.bin",
        models_root / "whisper" / "preprocessor_config.json",
        models_root / "sd-vae" / "config.json",
        models_root / "sd-vae" / "diffusion_pytorch_model.bin",
        models_root / "dwpose" / "dw-ll_ucoco_384.pth",
        models_root / "face-parse-bisent" / "79999_iter.pth",
        models_root / "face-parse-bisent" / "resnet18-5c106cde.pth",
    ]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        missing = [str(path) for path in required if not path.is_file()]
        return {"ok": not missing, "service": "musetalk", "busy": lock.locked(), "missing_models": missing}

    @app.post("/v1/lipsync")
    def lipsync(video: UploadFile = File(), audio: UploadFile = File()) -> Response:
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="MuseTalk is busy; retry after the current render finishes")
        started_at = time.monotonic()
        try:
            missing = [str(path) for path in required if not path.is_file()]
            if missing:
                raise HTTPException(status_code=503, detail={"missing_models": missing})
            with tempfile.TemporaryDirectory(prefix="gvs-musetalk-") as temporary:
                root = Path(temporary)
                video_path = root / "input.mp4"
                audio_path = root / "dialogue.wav"
                _copy_limited(video.file, video_path)
                _copy_limited(audio.file, audio_path)
                result_dir = root / "results"
                config_path = root / "inference.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "task": {
                                "video_path": str(video_path),
                                "audio_path": str(audio_path),
                                "result_name": "result.mp4",
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                command = [
                    python,
                    "-m",
                    "scripts.inference",
                    "--inference_config",
                    str(config_path),
                    "--result_dir",
                    str(result_dir),
                    "--unet_config",
                    str(models_root / "musetalkV15" / "musetalk.json"),
                    "--unet_model_path",
                    str(models_root / "musetalkV15" / "unet.pth"),
                    "--whisper_dir",
                    str(models_root / "whisper"),
                    "--use_float16",
                    "--version",
                    "v15",
                ]
                completed = subprocess.run(command, cwd=source_root, capture_output=True, text=True, timeout=timeout)
                result = result_dir / "v15" / "result.mp4"
                if completed.returncode != 0 or not result.is_file():
                    detail = (completed.stderr or completed.stdout).strip()[-2000:]
                    raise HTTPException(status_code=500, detail=f"MuseTalk inference failed: {detail}")
                payload = result.read_bytes()
                if len(payload) > MAX_UPLOAD_BYTES or b"ftyp" not in payload[:64]:
                    raise HTTPException(status_code=500, detail="MuseTalk produced an invalid MP4")
                elapsed = time.monotonic() - started_at
                print(f"MuseTalk inference completed in {elapsed:.2f}s; output_bytes={len(payload)}", flush=True)
                return Response(
                    payload,
                    media_type="video/mp4",
                    headers={
                        "Cache-Control": "no-store",
                        "X-GVS-Inference-Seconds": f"{elapsed:.2f}",
                        "X-GVS-Output-Bytes": str(len(payload)),
                    },
                )
        except ValueError as error:
            raise HTTPException(status_code=413, detail=str(error)) from error
        finally:
            lock.release()

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--models-root", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--port", type=int, default=9881)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    if not (args.source_root / "musetalk").is_dir():
        raise SystemExit("MuseTalk source root is invalid")
    if not shutil.which(args.python) and not Path(args.python).is_file():
        raise SystemExit("MuseTalk Python executable is unavailable")
    app = build_app(args.source_root, args.models_root, args.python, args.timeout)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
