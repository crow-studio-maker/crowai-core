import os

from waitress import serve

from app import app

serve(app, host=os.getenv("CROWAI_HOST", "127.0.0.1"), port=int(os.getenv("CROWAI_PORT", "5000")), threads=8)
