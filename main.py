import os
import json
import math
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

APP_NAME = "ChoirX AudibleFix API"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "90"))
TMP_ROOT = Path(os.getenv("TMPDIR", "/tmp")) / "choirx_audiblefix"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME, version="4.0.0")

origins_raw = os.getenv("CORS_ORIGINS", "*").strip()
allow_origins = ["*"] if origins_raw == "*" else [o.strip() for o in origins_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def run_cmd(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Audio render timed out. Try a shorter song clip.")


def ensure_ffmpeg() -> dict:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    return {"ffmpeg": bool(ffmpeg), "ffprobe": bool(ffprobe), "ffmpeg_path": ffmpeg, "ffprobe_path": ffprobe}


def probe_duration(path: Path) -> Optional[float]:
    if not shutil.which("ffprobe"):
        return None
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    proc = run_cmd(cmd, timeout=30)
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
        return float(payload.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return None


def safe_ext(filename: str, content_type: str | None) -> str:
    name = (filename or "upload").lower()
    for ext in [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"]:
        if name.endswith(ext):
            return ext
    if content_type:
        if "wav" in content_type:
            return ".wav"
        if "mpeg" in content_type or "mp3" in content_type:
            return ".mp3"
        if "ogg" in content_type:
            return ".ogg"
    return ".audio"


def pitch_chain(label: str, semitone: float, volume: float, delay_ms: int, pan: str) -> str:
    # Pitch shift with ffmpeg native filters: asetrate changes pitch/speed, atempo restores duration.
    factor = 2 ** (semitone / 12.0)
    new_rate = max(8000, int(44100 * factor))
    tempo = 1.0 / factor
    # atempo works safely in this range for our semitones.
    return (
        f"[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        f"asetrate={new_rate},aresample=44100,atempo={tempo:.6f},"
        f"volume={volume:.3f},adelay={delay_ms}|{delay_ms},{pan}[{label}]"
    )


def style_settings(style: str, strength: str) -> dict:
    style = (style or "gospel").lower().strip()
    strength = (strength or "strong").lower().strip()

    # Volumes intentionally strong because the previous version sounded unchanged.
    if strength == "light":
        dry, harm, octave, bass = 0.50, 0.34, 0.22, 0.16
    elif strength == "extreme":
        dry, harm, octave, bass = 0.18, 0.62, 0.48, 0.36
    else:
        dry, harm, octave, bass = 0.28, 0.50, 0.36, 0.25

    if style == "cathedral":
        return {
            "dry": dry,
            "layers": [
                ("h1", 3, harm, 22, "pan=stereo|c0=0.92*c0|c1=0.60*c1"),
                ("h2", 7, harm * 0.94, 42, "pan=stereo|c0=0.55*c0|c1=0.95*c1"),
                ("h3", 12, octave, 68, "pan=stereo|c0=0.62*c0|c1=0.70*c1"),
                ("h4", -12, bass, 88, "pan=stereo|c0=0.70*c0|c1=0.62*c1"),
            ],
            "post": "highpass=f=90,lowpass=f=10500,aecho=0.82:0.88:90|180|320:0.30|0.22|0.16,acompressor=threshold=-19dB:ratio=3.2:attack=18:release=220,alimiter=limit=0.92",
        }
    if style == "cinematic":
        return {
            "dry": dry * 0.85,
            "layers": [
                ("h1", 3, harm, 18, "pan=stereo|c0=0.85*c0|c1=0.65*c1"),
                ("h2", 7, harm, 36, "pan=stereo|c0=0.55*c0|c1=0.92*c1"),
                ("h3", 12, octave * 1.15, 58, "pan=stereo|c0=0.78*c0|c1=0.78*c1"),
                ("h4", -12, bass * 1.15, 80, "pan=stereo|c0=0.76*c0|c1=0.70*c1"),
            ],
            "post": "highpass=f=70,lowpass=f=12000,aecho=0.70:0.88:70|150|260:0.28|0.20|0.12,acompressor=threshold=-18dB:ratio=3.5:attack=15:release=260,alimiter=limit=0.92",
        }
    if style == "afrogospel":
        return {
            "dry": dry + 0.05,
            "layers": [
                ("h1", 4, harm, 14, "pan=stereo|c0=0.90*c0|c1=0.68*c1"),
                ("h2", 7, harm * 0.90, 28, "pan=stereo|c0=0.62*c0|c1=0.90*c1"),
                ("h3", 12, octave * 0.75, 44, "pan=stereo|c0=0.70*c0|c1=0.70*c1"),
                ("h4", -5, bass * 0.65, 55, "pan=stereo|c0=0.74*c0|c1=0.70*c1"),
            ],
            "post": "highpass=f=95,lowpass=f=12500,aecho=0.58:0.80:45|95:0.22|0.14,acompressor=threshold=-17dB:ratio=2.8:attack=10:release=170,alimiter=limit=0.94",
        }
    # default gospel / studio choir
    return {
        "dry": dry,
        "layers": [
            ("h1", 3, harm, 16, "pan=stereo|c0=0.92*c0|c1=0.62*c1"),
            ("h2", 7, harm * 0.96, 34, "pan=stereo|c0=0.58*c0|c1=0.92*c1"),
            ("h3", 12, octave, 54, "pan=stereo|c0=0.70*c0|c1=0.70*c1"),
            ("h4", -12, bass, 72, "pan=stereo|c0=0.70*c0|c1=0.65*c1"),
        ],
        "post": "highpass=f=80,lowpass=f=11500,aecho=0.65:0.84:55|120|210:0.26|0.18|0.10,acompressor=threshold=-18dB:ratio=3:attack=12:release=210,alimiter=limit=0.93",
    }


def build_filter(style: str, strength: str) -> str:
    settings = style_settings(style, strength)
    parts = [
        f"[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume={settings['dry']:.3f},highpass=f=80,lowpass=f=12000[base]"
    ]
    labels = ["[base]"]
    for label, semitone, volume, delay_ms, pan in settings["layers"]:
        parts.append(pitch_chain(label, semitone, volume, delay_ms, pan))
        labels.append(f"[{label}]")
    mix_inputs = "".join(labels)
    parts.append(f"{mix_inputs}amix=inputs={len(labels)}:duration=first:dropout_transition=0,{settings['post']},loudnorm=I=-15:TP=-1.5:LRA=10[out]")
    return ";".join(parts)


@app.get("/")
def root():
    return {
        "ok": True,
        "service": APP_NAME,
        "message": "Backend is running. Use /api/health or connect your Netlify frontend.",
    }


@app.get("/api/health")
def health():
    tools = ensure_ffmpeg()
    return {
        "ok": True,
        "service": APP_NAME,
        "mode": "audible-choir-harmony-ffmpeg",
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_audio_seconds": MAX_AUDIO_SECONDS,
        "cors_origins": allow_origins,
        **tools,
    }


@app.post("/api/render")
async def render_audio(
    file: UploadFile = File(...),
    style: str = Form("gospel"),
    strength: str = Form("strong"),
):
    tools = ensure_ffmpeg()
    if not tools["ffmpeg"]:
        raise HTTPException(status_code=500, detail="ffmpeg is not installed on this server.")

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    suffix = safe_ext(file.filename or "upload", file.content_type)
    job_id = uuid.uuid4().hex[:12]
    work_dir = TMP_ROOT / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    input_path = work_dir / f"input{suffix}"
    output_path = work_dir / "choirx-audible-choir.mp3"

    try:
        written = 0
        with input_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail=f"File too large. Keep it under {MAX_UPLOAD_MB}MB on this Render plan.")
                out.write(chunk)

        duration = probe_duration(input_path)
        if duration and duration > MAX_AUDIO_SECONDS:
            raise HTTPException(status_code=413, detail=f"Audio is too long for this Render plan. Keep it under {MAX_AUDIO_SECONDS} seconds.")

        filter_complex = build_filter(style, strength)
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-i", str(input_path),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-vn",
            "-codec:a", "libmp3lame",
            "-b:a", "192k",
            "-ar", "44100",
            str(output_path),
        ]
        proc = run_cmd(cmd, timeout=180)
        if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 1024:
            detail = proc.stderr[-2500:] if proc.stderr else "Unknown ffmpeg error."
            raise HTTPException(status_code=500, detail=f"Render failed: {detail}")

        headers = {
            "X-ChoirX-Mode": "audible-choir-harmony-ffmpeg",
            "X-ChoirX-Style": style,
            "X-ChoirX-Strength": strength,
        }
        return FileResponse(
            output_path,
            media_type="audio/mpeg",
            filename=f"choirx-{style}-{strength}.mp3",
            headers=headers,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}")
