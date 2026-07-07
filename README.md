# ChoirX LongFix Backend

Deploy this `backend` folder to Render.

Render settings if using the full repo:

```text
Environment: Docker
Root Directory: backend
Dockerfile Path: Dockerfile
Health Check Path: /api/health
```

Environment variables:

```text
CORS_ORIGINS=*
MAX_UPLOAD_MB=25
MAX_AUDIO_SECONDS=240
```

This version no longer rejects a full song just because it is longer than 90 seconds. It auto-processes only the selected duration, up to `MAX_AUDIO_SECONDS`.

Health check should show:

```text
mode: longfix-auto-trim-audible-choir-ffmpeg
```
