"""Development entry point.

Run with:  python run.py
For production, use gunicorn with threaded workers (see README).
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    # threaded=True is required so streaming responses don't block other requests.
    app.run(host=host, port=port, debug=debug, threaded=True)
