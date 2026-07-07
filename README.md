# ChoirX Lite Backend for Render

Low-memory Render backend for ChoirX. This version uses ffmpeg only. It does not use numpy or large in-memory audio arrays.

## Render settings

If this backend is inside a full repo:

- Environment: Docker
- Root Directory: backend
- Dockerfile Path: Dockerfile
- Health Check Path: /api/health

If this backend is the whole repo, leave Root Directory blank.

## Environment variables

```text
CORS_ORIGINS=*
MAX_UPLOAD_MB=8
MAX_AUDIO_SECONDS=75
```

After deploy, open:

```text
https://your-service.onrender.com/api/health
```

You should see:

```text
mode: ffmpeg-lite-no-numpy-low-memory
```
