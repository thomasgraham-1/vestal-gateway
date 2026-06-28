# Vestal gateway — stdlib only, no pip install needed.
FROM python:3.12-slim

WORKDIR /app
COPY gateway.py adduser.py seed_demo.py dashboard.html login.html ./

# HOST/PORT: the gateway binds 0.0.0.0:$PORT when PORT is set (Cloud Run / Fly / etc).
ENV PORT=8788 VESTAL_SECURE=1
# Set at deploy time:
#   VESTAL_SESSION_SECRET   strong random secret (REQUIRED in production)
#   ANTHROPIC_API_KEY / GEMINI_API_KEY   only if the proxy should forward real calls
EXPOSE 8788
CMD ["python3", "gateway.py"]
