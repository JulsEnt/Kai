from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

APP_NAME = "ChoirX Lite API"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/choirx_outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".webm"}

STYLE_PRESETS: Dict[str, Dict[str, float]] = {
    "gospel": {"mix": 0.86, "reverb": 0.68, "bass": 1.00, "treble": 1.06, "extra": 0.12},
    "cathedral": {"mix": 0.78, "reverb": 0.92, "bass": 0.95, "treble": 0.96, "extra": 0.08},
    "cinematic": {"mix": 0.90, "reverb": 0.82, "bass": 1.03, "treble": 1.00, "extra": 0.10},
    "acapella": {"mix": 0.92, "reverb": 0.48, "bass": 0.88, "treble": 1.02, "extra": 0.05},
    "afro-gospel": {"mix": 0.84, "reverb": 0.58, "bass": 1.06, "treble": 1.09, "extra": 0.14},
}

VOICE_PRESETS: Dict[str, Dict[str, float]] = {
    "mixed": {"low": 1.00, "high": 1.00},
    "male": {"low": 1.22, "high": 0.72},
    "female": {"low": 0.72, "high": 1.22},
}

HARMONY_PRESETS = {
    "soft": [-3, 4],
    "warm": [-5, -3, 4, 7],
    "big": [-12, -5, -3, 4, 7, 12],
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_origins() -> List[str]:
    raw = os.getenv("CORS_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def max_upload_mb() -> int:
    return int(os.getenv("MAX_UPLOAD_MB", "8"))


def max_upload_bytes() -> int:
    return max_upload_mb() * 1024 * 1024


def max_audio_seconds() -> int:
    return int(os.getenv("MAX_AUDIO_SECONDS", "75"))


def ffmpeg_bin() -> str:
    return os.getenv("FFMPEG_BIN", "ffmpeg")


app = FastAPI(title=APP_NAME, version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.get("/")
def root() -> dict:
    return {
        "ok": True,
        "service": APP_NAME,
        "message": "Backend is running. Use your Netlify frontend and paste this Render URL there.",
        "health": "/api/health",
        "render": "/api/render",
    }


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "service": APP_NAME,
        "mode": "ffmpeg-lite-no-numpy-low-memory",
        "cors_origins": parse_origins(),
        "max_upload_mb": max_upload_mb(),
        "max_audio_seconds": max_audio_seconds(),
        "timestamp": int(time.time()),
    }


@app.get("/api/debug")
def debug() -> dict:
    return {
        "ok": True,
        "service": APP_NAME,
        "output_dir": str(OUTPUT_DIR),
        "cors_origins": parse_origins(),
        "max_upload_mb": max_upload_mb(),
        "max_audio_seconds": max_audio_seconds(),
    }


@app.options("/{full_path:path}")
def options_handler(full_path: str) -> JSONResponse:
    return JSONResponse({"ok": True, "path": full_path})


def run_command(cmd: List[str], timeout_seconds: int = 180) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail="Audio rendering timed out. Try a shorter audio file, ideally under 60 seconds on Render free.",
        ) from exc


def probe_duration(input_path: Path) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(input_path),
    ]
    completed = run_command(cmd, timeout_seconds=30)
    if completed.returncode != 0:
        return None
    try:
        data = json.loads(completed.stdout or "{}")
        return float(data.get("format", {}).get("duration"))
    except Exception:
        return None


def build_filter(style: str, voices: str, harmony: str, intensity: float, reverb: float) -> str:
    sample_rate = 44100
    style_data = STYLE_PRESETS.get(style, STYLE_PRESETS["gospel"])
    voice_data = VOICE_PRESETS.get(voices, VOICE_PRESETS["mixed"])
    intervals = HARMONY_PRESETS.get(harmony, HARMONY_PRESETS["warm"])

    intensity = clamp(float(intensity), 0.0, 1.0)
    reverb = clamp(float(reverb), 0.0, 1.0)
    choir_gain = style_data["mix"] * (0.35 + intensity * 0.75)
    reverb_gain = style_data["reverb"] * (0.20 + reverb * 0.85)
    low_gain = voice_data["low"]
    high_gain = voice_data["high"]

    # Keep the filter small for Render free memory.
    selected_intervals = intervals[:4] if harmony != "big" else intervals[:5]
    split_count = 1 + len(selected_intervals) + 2
    labels = ["base"] + [f"h{i}" for i in range(len(selected_intervals))] + ["d1", "d2"]
    split_labels = "".join(f"[{label}]" for label in labels)

    parts: List[str] = []
    parts.append(
        f"[0:a]aresample={sample_rate},aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"atrim=0:{max_audio_seconds()},asetpts=N/SR/TB,volume=0.78,"
        f"asplit={split_count}{split_labels}"
    )

    mix_inputs = ["[base]" ]

    for idx, semitone in enumerate(selected_intervals):
        factor = 2 ** (semitone / 12.0)
        atempo = 1.0 / factor
        # atempo must remain between 0.5 and 2.0. For our intervals it is safe.
        layer_gain = 0.11 * choir_gain
        if semitone < 0:
            layer_gain *= low_gain
        else:
            layer_gain *= high_gain
        delay_ms = 25 + idx * 17
        label_out = f"ho{idx}"
        parts.append(
            f"[h{idx}]asetrate={sample_rate * factor:.5f},aresample={sample_rate},"
            f"atempo={atempo:.5f},adelay={delay_ms}:all=1,volume={layer_gain:.4f}[{label_out}]"
        )
        mix_inputs.append(f"[{label_out}]")

    # Two chorus/doubling layers without heavy pitch work.
    parts.append(f"[d1]adelay=38:all=1,volume={0.12 * choir_gain:.4f}[do1]")
    parts.append(f"[d2]adelay=74:all=1,volume={0.09 * choir_gain:.4f}[do2]")
    mix_inputs += ["[do1]", "[do2]"]

    echo_delays = "60|140|260"
    echo_decays = f"{0.18 * reverb_gain:.3f}|{0.12 * reverb_gain:.3f}|{0.08 * reverb_gain:.3f}"
    n_inputs = len(mix_inputs)
    final = (
        "".join(mix_inputs)
        + f"amix=inputs={n_inputs}:duration=first:normalize=0,"
        + f"bass=g={2.2 * (style_data['bass'] - 1.0):.3f},"
        + f"treble=g={2.2 * (style_data['treble'] - 1.0):.3f},"
        + f"aecho=0.8:0.88:{echo_delays}:{echo_decays},"
        + "acompressor=threshold=0.20:ratio=2.0:attack=20:release=250,"
        + "alimiter=limit=0.94,volume=0.92[out]"
    )
    parts.append(final)
    return ";".join(parts)


def render_with_ffmpeg(input_path: Path, output_path: Path, style: str, voices: str, harmony: str, intensity: float, reverb: float) -> Dict[str, object]:
    duration = probe_duration(input_path)
    if duration is not None and duration < 1.0:
        raise HTTPException(status_code=400, detail="Audio is too short. Upload a longer audio clip.")

    filter_complex = build_filter(style, voices, harmony, intensity, reverb)
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(input_path),
        "-t",
        str(max_audio_seconds()),
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    completed = run_command(cmd, timeout_seconds=max(120, max_audio_seconds() * 3))
    if completed.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 1000:
        error = (completed.stderr or completed.stdout or "ffmpeg could not render this audio.").strip()
        raise HTTPException(status_code=400, detail=error[-1000:])

    return {
        "duration_seconds": round(min(duration or max_audio_seconds(), max_audio_seconds()), 2),
        "style": style,
        "voices": voices,
        "harmony": harmony,
        "intensity": intensity,
        "reverb": reverb,
        "engine": "ffmpeg-lite-no-numpy",
        "output_bytes": output_path.stat().st_size,
    }


@app.post("/api/render")
async def render_song(
    request: Request,
    file: UploadFile = File(...),
    style: str = Form("gospel"),
    voices: str = Form("mixed"),
    harmony: str = Form("warm"),
    intensity: float = Form(0.72),
    reverb: float = Form(0.68),
):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_upload_bytes() + 1_000_000:
        raise HTTPException(status_code=413, detail=f"File is too large. Limit is {max_upload_mb()}MB on this backend.")

    filename = Path(file.filename or "song.audio").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        suffix = ".audio"

    job_id = uuid.uuid4().hex[:16]

    with tempfile.TemporaryDirectory(prefix="choirx_lite_") as tmp_dir:
        input_path = Path(tmp_dir) / f"input{suffix}"
        output_path = OUTPUT_DIR / f"choirx_{job_id}.wav"

        total = 0
        try:
            with input_path.open("wb") as buffer:
                while True:
                    chunk = await file.read(512 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_upload_bytes():
                        raise HTTPException(status_code=413, detail=f"File is too large. Limit is {max_upload_mb()}MB on this backend.")
                    buffer.write(chunk)
        finally:
            await file.close()

        if total < 1024:
            raise HTTPException(status_code=400, detail="No valid audio file was uploaded.")

        metadata = render_with_ffmpeg(
            input_path=input_path,
            output_path=output_path,
            style=style if style in STYLE_PRESETS else "gospel",
            voices=voices if voices in VOICE_PRESETS else "mixed",
            harmony=harmony if harmony in HARMONY_PRESETS else "warm",
            intensity=clamp(float(intensity), 0.0, 1.0),
            reverb=clamp(float(reverb), 0.0, 1.0),
        )

    return {
        "ok": True,
        "job_id": job_id,
        "download_url": f"/api/download/{job_id}",
        "metadata": metadata,
    }


@app.get("/api/download/{job_id}")
def download(job_id: str):
    clean = job_id.replace("-", "")
    if not clean.isalnum() or len(clean) > 64:
        raise HTTPException(status_code=400, detail="Invalid job ID.")
    path = OUTPUT_DIR / f"choirx_{job_id}.wav"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Rendered file not found. It may have expired after the server restarted.")
    return FileResponse(
        path,
        media_type="audio/wav",
        filename="choirx-render.wav",
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"ok": False, "error": f"Server error: {exc}"})
