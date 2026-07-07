import json
import math
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

APP_NAME = "ChoirX LongFix API"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "240"))
TMP_ROOT = Path(os.getenv("TMPDIR", "/tmp")) / "choirx_longfix"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME, version="5.0.0")

origins_raw = os.getenv("CORS_ORIGINS", "*").strip()
allow_origins = ["*"] if origins_raw == "*" else [o.strip() for o in origins_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-ChoirX-Mode",
        "X-ChoirX-Style",
        "X-ChoirX-Strength",
        "X-ChoirX-Original-Seconds",
        "X-ChoirX-Processed-Seconds",
        "X-ChoirX-Trimmed",
    ],
)


def run_cmd(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
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
        raise HTTPException(status_code=504, detail="Audio render timed out. Try 90 or 120 seconds first.")


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
        c = content_type.lower()
        if "wav" in c:
            return ".wav"
        if "mpeg" in c or "mp3" in c:
            return ".mp3"
        if "ogg" in c:
            return ".ogg"
        if "flac" in c:
            return ".flac"
    return ".audio"


def clamp_float(value: str | float | int | None, default: float, min_value: float, max_value: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except Exception:
        parsed = default
    if math.isnan(parsed) or math.isinf(parsed):
        parsed = default
    return max(min_value, min(max_value, parsed))


def pitch_chain(label: str, semitone: float, volume: float, delay_ms: int, pan: str) -> str:
    # Native ffmpeg pitch effect. asetrate changes pitch; atempo corrects timing.
    factor = 2 ** (semitone / 12.0)
    new_rate = max(8000, int(44100 * factor))
    tempo = 1.0 / factor
    return (
        f"[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        f"asetrate={new_rate},aresample=44100,atempo={tempo:.6f},"
        f"volume={volume:.3f},adelay={delay_ms}|{delay_ms},{pan}[{label}]"
    )


def style_settings(style: str, strength: str) -> dict:
    style = (style or "gospel").lower().strip()
    strength = (strength or "extreme").lower().strip()

    # Strong defaults because earlier builds sounded too close to the original.
    if strength == "light":
        dry, harm, octave, bass = 0.48, 0.35, 0.22, 0.18
    elif strength == "strong":
        dry, harm, octave, bass = 0.26, 0.54, 0.40, 0.30
    else:  # extreme
        dry, harm, octave, bass = 0.14, 0.68, 0.52, 0.42

    if style == "cathedral":
        return {
            "dry": dry,
            "layers": [
                ("h1", 3, harm, 22, "pan=stereo|c0=0.95*c0|c1=0.55*c1"),
                ("h2", 7, harm * 0.96, 44, "pan=stereo|c0=0.50*c0|c1=0.98*c1"),
                ("h3", 12, octave, 74, "pan=stereo|c0=0.68*c0|c1=0.72*c1"),
                ("h4", -12, bass, 102, "pan=stereo|c0=0.76*c0|c1=0.62*c1"),
                ("h5", -5, bass * 0.55, 128, "pan=stereo|c0=0.62*c0|c1=0.76*c1"),
            ],
            "post": "highpass=f=85,lowpass=f=9800,aecho=0.84:0.90:110|220|360:0.34|0.24|0.17,acompressor=threshold=-20dB:ratio=3.4:attack=18:release=240,alimiter=limit=0.92",
        }
    if style == "cinematic":
        return {
            "dry": dry * 0.82,
            "layers": [
                ("h1", 3, harm, 18, "pan=stereo|c0=0.88*c0|c1=0.62*c1"),
                ("h2", 7, harm, 36, "pan=stereo|c0=0.52*c0|c1=0.94*c1"),
                ("h3", 12, octave * 1.18, 58, "pan=stereo|c0=0.78*c0|c1=0.78*c1"),
                ("h4", -12, bass * 1.18, 84, "pan=stereo|c0=0.78*c0|c1=0.70*c1"),
                ("h5", -7, bass * 0.65, 112, "pan=stereo|c0=0.65*c0|c1=0.78*c1"),
            ],
            "post": "highpass=f=65,lowpass=f=12000,aecho=0.72:0.88:80|160|280:0.30|0.21|0.13,acompressor=threshold=-18dB:ratio=3.6:attack=15:release=260,alimiter=limit=0.92",
        }
    if style == "afrogospel":
        return {
            "dry": dry + 0.05,
            "layers": [
                ("h1", 4, harm, 14, "pan=stereo|c0=0.92*c0|c1=0.66*c1"),
                ("h2", 7, harm * 0.92, 28, "pan=stereo|c0=0.60*c0|c1=0.92*c1"),
                ("h3", 12, octave * 0.80, 46, "pan=stereo|c0=0.72*c0|c1=0.72*c1"),
                ("h4", -5, bass * 0.72, 62, "pan=stereo|c0=0.78*c0|c1=0.72*c1"),
            ],
            "post": "highpass=f=95,lowpass=f=12500,aecho=0.60:0.80:48|100:0.24|0.16,acompressor=threshold=-17dB:ratio=2.8:attack=10:release=175,alimiter=limit=0.94",
        }
    return {
        "dry": dry,
        "layers": [
            ("h1", 3, harm, 16, "pan=stereo|c0=0.92*c0|c1=0.60*c1"),
            ("h2", 7, harm * 0.98, 34, "pan=stereo|c0=0.56*c0|c1=0.94*c1"),
            ("h3", 12, octave, 56, "pan=stereo|c0=0.70*c0|c1=0.70*c1"),
            ("h4", -12, bass, 78, "pan=stereo|c0=0.74*c0|c1=0.64*c1"),
            ("h5", -5, bass * 0.55, 104, "pan=stereo|c0=0.62*c0|c1=0.76*c1"),
        ],
        "post": "highpass=f=80,lowpass=f=11500,aecho=0.68:0.85:60|130|230:0.28|0.19|0.11,acompressor=threshold=-18dB:ratio=3.1:attack=12:release=220,alimiter=limit=0.93",
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


def cleanup_dir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


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
        "mode": "longfix-auto-trim-audible-choir-ffmpeg",
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_audio_seconds": MAX_AUDIO_SECONDS,
        "cors_origins": allow_origins,
        **tools,
    }


@app.post("/api/render")
async def render_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    style: str = Form("gospel"),
    strength: str = Form("extreme"),
    render_seconds: str = Form("240"),
    start_seconds: str = Form("0"),
):
    tools = ensure_ffmpeg()
    if not tools["ffmpeg"]:
        raise HTTPException(status_code=500, detail="ffmpeg is not installed on this server.")

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    requested_seconds = clamp_float(render_seconds, default=min(240, MAX_AUDIO_SECONDS), min_value=10, max_value=MAX_AUDIO_SECONDS)
    start_at = clamp_float(start_seconds, default=0, min_value=0, max_value=24 * 60 * 60)

    suffix = safe_ext(file.filename or "upload", file.content_type)
    job_id = uuid.uuid4().hex[:12]
    work_dir = TMP_ROOT / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    input_path = work_dir / f"input{suffix}"
    output_path = work_dir / "choirx-longfix-choir.mp3"

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
        source_seconds = duration if duration and duration > 0 else 0
        if source_seconds and start_at >= source_seconds:
            raise HTTPException(status_code=400, detail="Start position is after the end of the audio.")

        available_from_start = max(0.1, source_seconds - start_at) if source_seconds else requested_seconds
        process_seconds = min(requested_seconds, MAX_AUDIO_SECONDS, available_from_start)
        process_seconds = max(1.0, process_seconds)
        trimmed = bool(source_seconds and (start_at > 0 or process_seconds < available_from_start - 0.5))

        filter_complex = build_filter(style, strength)
        # -ss and -t before input reduces CPU work and avoids rejecting full songs.
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-ss", f"{start_at:.3f}",
            "-t", f"{process_seconds:.3f}",
            "-i", str(input_path),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-vn",
            "-codec:a", "libmp3lame",
            "-b:a", "192k",
            "-ar", "44100",
            str(output_path),
        ]
        timeout = int(max(180, process_seconds * 3 + 60))
        proc = run_cmd(cmd, timeout=timeout)
        if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 1024:
            detail = proc.stderr[-2500:] if proc.stderr else "Unknown ffmpeg error."
            raise HTTPException(status_code=500, detail=f"Render failed: {detail}")

        headers = {
            "X-ChoirX-Mode": "longfix-auto-trim-audible-choir-ffmpeg",
            "X-ChoirX-Style": style,
            "X-ChoirX-Strength": strength,
            "X-ChoirX-Original-Seconds": f"{source_seconds:.2f}" if source_seconds else "unknown",
            "X-ChoirX-Processed-Seconds": f"{process_seconds:.2f}",
            "X-ChoirX-Trimmed": "true" if trimmed else "false",
        }
        background_tasks.add_task(cleanup_dir, work_dir)
        return FileResponse(
            output_path,
            media_type="audio/mpeg",
            filename=f"choirx-{style}-{strength}.mp3",
            headers=headers,
            background=background_tasks,
        )
    except HTTPException:
        cleanup_dir(work_dir)
        raise
    except Exception as exc:
        cleanup_dir(work_dir)
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}")
