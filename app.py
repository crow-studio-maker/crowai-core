"""Compatibility WSGI entry point for CrowAI Core."""
from crowai.application import create_app

app = create_app()

if __name__ == "__main__":
    import os

    app.run(
        host=os.getenv("CROWAI_HOST", "127.0.0.1"),
        port=int(os.getenv("CROWAI_PORT", "5000")),
        threaded=True,
    )
