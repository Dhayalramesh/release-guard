"""Tiny demo service that Release Guard deploys."""
import os

from fastapi import FastAPI, Response

app = FastAPI(title="release-guard demo service")

VERSION = os.getenv("APP_VERSION", "v0.0.0")
BROKEN = os.getenv("APP_BROKEN", "0") == "1"  # simulates a bad release


@app.get("/health")
def health(response: Response):
    if BROKEN:
        response.status_code = 500
        return {"status": "error", "version": VERSION}
    return {"status": "ok", "version": VERSION}


@app.get("/version")
def version():
    return {"version": VERSION}
