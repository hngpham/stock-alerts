# Contributing to stock-alerts

Thanks for helping!

## Getting started (dev)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
# frontend served at /static
