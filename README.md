# ChoirX AudibleFix Backend

Deploy this folder on Render as a Docker web service.

Render settings if this is inside a full repo:

```text
Environment: Docker
Root Directory: backend
Dockerfile Path: Dockerfile
Health Check Path: /api/health
```

Environment variables:

```text
CORS_ORIGINS=*
MAX_UPLOAD_MB=12
MAX_AUDIO_SECONDS=90
```

After deploy, test:

```text
https://your-render-url.onrender.com/api/health
```

It should show:

```text
mode: audible-choir-harmony-ffmpeg
```
