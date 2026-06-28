# Vestal gateway — stdlib only, no pip install needed.
FROM python:3.12-slim

WORKDIR /app
COPY gateway.py adduser.py seed_demo.py dashboard.html login.html ./

# The gateway binds 0.0.0.0:$PORT when PORT is set (Cloud Run / Fly inject it).
# Don't hardcode PORT — let the platform set it (Cloud Run defaults to 8080).
ENV PYTHONUNBUFFERED=1
# Set at deploy time:
#   VESTAL_SESSION_SECRET   strong random secret (REQUIRED in production)
#   VESTAL_SECURE=1         Secure cookies when served over HTTPS
#   VESTAL_DEMO=1           seed a fictional fleet + admin login (demo only)
#   ANTHROPIC_API_KEY / GEMINI_API_KEY   only if the proxy should forward real calls
EXPOSE 8080
CMD ["python3", "gateway.py"]
