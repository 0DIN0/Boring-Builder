"""Gunicorn configuration for Boring Builder.

Streaming (SSE) responses require threaded workers so one slow generation does
not block other requests. A single process with several threads is the right
shape here: the workload is I/O-bound (waiting on the Ollama daemon), and using
one process keeps the SQLite database access simple.
"""
import os

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '5001')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("WEB_THREADS", "8"))
worker_class = "gthread"

# Generations and model pulls can run for a long time.
timeout = int(os.environ.get("WEB_TIMEOUT", "0")) or 0  # 0 = disable worker timeout
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
proc_name = "boring-builder"
