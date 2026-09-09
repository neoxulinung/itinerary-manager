FROM python:3.12-slim

# Same lesson as line-team-recorder: stdout is block-buffered when not attached to a TTY (i.e.
# always, in a container), so print()-based error logs (log_error in log.py) can sit unflushed
# indefinitely - Cloud Logging would show nothing until the buffer happens to fill.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ .

# Cloud Run injects $PORT at runtime - shell form so it actually expands.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
