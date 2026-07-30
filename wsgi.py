"""WSGI entry point for production servers (gunicorn/uwsgi).

    gunicorn --config gunicorn_conf.py wsgi:app
"""
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app import create_app

app = create_app()
