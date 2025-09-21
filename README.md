# Stock Alert (FastAPI + SQLite + Minimal Frontend)

Fast, self-hosted stock alert watcher. Caches quotes on a schedule and posts
Discord notifications for price/percent/earnings-day rules. Frontend is a tiny
vanilla JS app for managing symbols, notes, ratings, and alerts.

![screenshot](docs/screenshot.png)

## Features
- 📈 Cached quotes, updated on a schedule (APScheduler)
- 🔔 Alerts: above/below price, % jump/drop, “earnings in N days”
- 🧠 Notes + 1–5 ⭐ ratings per symbol
- 🗂️ Watch vs Archived groups
- 🧵 Discord webhook notifications
- 🔒 Zero keys in repo: `.env` driven
- 🐳 Docker & docker-compose

## Architecture
- **Backend:** FastAPI, SQLite, APScheduler (`backend/`)
- **Frontend:** static HTML/CSS/JS served by FastAPI (`frontend/`)
- **Providers:** pluggable quote providers in `quote_sources/`

