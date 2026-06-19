#!/usr/bin/env python3
"""
WSGI entry point for production servers (Gunicorn, uWSGI, etc.)
"""
from server import app

if __name__ == "__main__":
    app.run()

