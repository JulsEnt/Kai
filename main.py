from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

APP_NAME = "ChoirX MemoryFix API"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/choirx_outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_STYLES = {"gospel", "cathedral", "cinematic", "acapella", "afro-gospel"}
ALLOWED_VOICES = {"mixed", "male", "female"}
ALLOWED_HARMONY = {"soft", "warm", "big"}


def parse_origins() -> List[str]:
    raw = os.getenv("CORS_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def max_upload_bytes() -> int:
    return int(os.getenv("MAX_UPLOAD_MB", "8")) * 1024 * 1024


def max_audio_seconds() -> int:
    return int(os.getenv("MAX_AUDIO_SECONDS", "75"))


def clean_value(value: str, allowed: set[str], default: str) -> str:
    return value if value in allowed else default


app = FastAPI(title=APP_NAME, version="2.0-memoryfix")
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> dict:
    return {
        "ok": True,
        "service": APP_NAME,
        "message": "Backend is running. Connect your Netlify frontend to this Render URL.",
        "health": "/api/health",
        "render_endpoint": "/api/render",
    }


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "service": APP_NAME,
        "version": "2.0-memoryfix",
        "max_upload_mb": int(os.getenv("MAX_UPLOAD_MB", "8")),
        "max_audio_seconds": max_audio_seconds(),
        "memory_mode": "ffmpeg-streaming-no-numpy",
    }


@app.get("/api/debug")
def debug() -> dict:
    return {
        "ok": True,
        "cors_origins": parse_origins(),
        "output_dir": str(OUTPUT_DIR),
        "max_upload_mb": int(os.getenv("MAX_UPLOAD_MB", "8")),
        "max_audio_seconds": max_audio_seconds(),
    }


def style_filter(style: str, voices: str, harmony: str, intensity: float, reverb: float, seconds: int) -> list[str]:
    """Build a low-memory ffmpeg filter_complex.

    This avoids loading the whole song into Python/Numpy. ffmpeg decodes, mixes,
    adds harmony-like pitch layers, chorus, echo/reverb and limiter directly.
    """
    intensity = max(0.0, min(1.0, float(intensity)))
    reverb = max(0.0, min(1.0, float(reverb)))

    # 22050Hz keeps the free Render memory stable while still sounding clean for preview.
    sr = 22050
    base_gain = 0.76 if style != "acapella" else 0.66
    choir_gain = 0.12 + (0.10 * intensity)

    if voices == "male":
        pitch_layers = [(-5, 0.18), (-12, 0.12), (-3, 0.10)]
    elif voices == "female":
        pitch_layers = [(4, 0.18), (7, 0.13), (12, 0.09)]
    else:
        pitch_layers = [(-5, 0.14), (-3, 0.12), (4, 0.13), (7, 0.09)]

    if harmony == "soft":
        pitch_layers = pitch_layers[:2]
    elif harmony == "big":
        pitch_layers = pitch_layers + [(-12, 0.07), (12, 0.07)]

    if style == "cathedral":
        echo_delays = "90|180|360|520"
        echo_decays = "0.23|0.18|0.13|0.09"
        chorus_depth = "0.45|0.35|0.28"
    elif style == "cinematic":
        echo_delays = "70|160|320|480"
        echo_decays = "0.20|0.15|0.11|0.08"
        chorus_depth = "0.40|0.32|0.25"
    elif style == "afro-gospel":
        echo_delays = "45|110|220|340"
        echo_decays = "0.17|0.12|0.08|0.05"
        chorus_depth = "0.35|0.25|0.20"
    elif style == "acapella":
        echo_delays = "55|130|250"
        echo_decays = "0.14|0.10|0.07"
        chorus_depth = "0.32|0.24|0.18"
    else:
        echo_delays = "60|140|280|420"
        echo_decays = "0.19|0.14|0.10|0.07"
        chorus_depth = "0.38|0.30|0.22"

    # Convert each semitone shift into asetrate + atempo pair.
    chains = [f"[0:a]atrim=0:{seconds},asetpts=N/SR/TB,aresample={sr},aformat=channel_layouts=stereo,volume={base_gain:.3f}[base]"]
    mix_inputs = ["[base]"]

    for i, (semi, gain) in enumerate(pitch_layers):
        factor = 2 ** (semi / 12)
        tempo = 1 / factor
        # atempo supports 0.5..100. These intervals remain in range.
        delay_ms = 18 + (i * 13)
        safe_gain = gain * choir_gain * 5.0
        chains.append(
            f"[base]asetrate={int(sr * factor)},aresample={sr},atempo={tempo:.5f},"
            f"adelay={delay_ms}|{delay_ms},volume={safe_gain:.3f}[h{i}]"
        )
        mix_inputs.append(f"[h{i}]")

    n_inputs = len(mix_inputs)
    room = max(0.05, min(0.85, reverb))
    final_chain = (
        f"{''.join(mix_inputs)}amix=inputs={n_inputs}:normalize=0,"
        f"chorus=0.55:0.75:45|60|75:{chorus_depth}:0.25|0.32|0.40:2|2.3|1.7,"
        f"aecho=0.75:{0.32 + room * 0.45:.3f}:{echo_delays}:{echo_decays},"
        "acompressor=threshold=0.12:ratio=3:attack=20:release=250,"
        "alimiter=limit=0.92,volume=0.94[out]"
    )
    chains.append(final_chain)
    return [";".join(chains), "[out]"]


def run_ffmpeg_render(input_path: Path, output_path: Path, style: str, voices: str, harmony: str, intensity: float, reverb: float) -> None:
    seconds = max_audio_seconds()
    filter_complex, map_name = style_filter(style, voices, harmony, intensity, reverb, seconds)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-filter_complex",
        filter_complex,
        "-map",
        map_name,
        "-ar",
        "22050",
        "-ac",
        "2",
        "-sample_fmt",
        "s16",
        str(output_path),
    ]
    completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=240)
    if completed.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 1000:
        detail = completed.stderr.strip() or "ffmpeg could not render this audio. Try a shorter MP3/WAV file."
        raise HTTPException(status_code=400, detail=detail[:700])


def cleanup_old_outputs(max_age_seconds: int = 60 * 60) -> None:
    now = time.time()
    try:
        for path in OUTPUT_DIR.glob("choirx_*.wav"):
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink(missing_ok=True)
    except Exception:
        pass


@app.post("/api/render")
async def render_song(
    request: Request,
    file: UploadFile = File(...),
    style: str = Form("gospel"),
    voices: str = Form("mixed"),
    harmony: str = Form("warm"),
    intensity: float = Form(0.70),
    reverb: float = Form(0.60),
):
    cleanup_old_outputs()
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_upload_bytes() + 1_000_000:
        raise HTTPException(status_code=413, detail=f"File too large. Use a file under {int(os.getenv('MAX_UPLOAD_MB', '8'))}MB on the free server.")

    safe_name = Path(file.filename or "song.audio").name
    suffix = Path(safe_name).suffix.lower() or ".audio"
    job_id = uuid.uuid4().hex[:16]

    style = clean_value(style, ALLOWED_STYLES, "gospel")
    voices = clean_value(voices, ALLOWED_VOICES, "mixed")
    harmony = clean_value(harmony, ALLOWED_HARMONY, "warm")

    with tempfile.TemporaryDirectory(prefix="choirx_") as tmp:
        input_path = Path(tmp) / f"input{suffix}"
        output_path = OUTPUT_DIR / f"choirx_{job_id}.wav"

        total = 0
        with input_path.open("wb") as buffer:
            while True:
                chunk = await file.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_upload_bytes():
                    raise HTTPException(status_code=413, detail=f"File too large. Use a file under {int(os.getenv('MAX_UPLOAD_MB', '8'))}MB on the free server.")
                buffer.write(chunk)

        if total == 0:
            raise HTTPException(status_code=400, detail="No audio file was uploaded.")

        run_ffmpeg_render(input_path, output_path, style, voices, harmony, intensity, reverb)

    return JSONResponse(
        {
            "ok": True,
            "job_id": job_id,
            "download_url": f"/api/download/{job_id}",
            "metadata": {
                "style": style,
                "voices": voices,
                "harmony": harmony,
                "intensity": round(float(intensity), 2),
                "reverb": round(float(reverb), 2),
                "max_seconds_used": max_audio_seconds(),
                "engine": "memoryfix-ffmpeg",
            },
        }
    )


@app.get("/api/download/{job_id}")
def download(job_id: str):
    if not job_id.replace("-", "").isalnum() or len(job_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid job id.")
    path = OUTPUT_DIR / f"choirx_{job_id}.wav"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Rendered file not found. It may have expired.")
    return FileResponse(
        path,
        media_type="audio/wav",
        filename="choirx-render.wav",
        headers={"Cache-Control": "no-store"},
    )


@app.options("/{full_path:path}")
def options_preflight(full_path: str):
    return JSONResponse({"ok": True})


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.detail})
