# PodScribe 🎙️

Privater Podcast-Transkriptions-Hub — abonniere Podcasts, transkribiere Folgen automatisch mit Gemini AI, durchsuche deine Bibliothek und erstelle journalistische Artikel aus den Inhalten.

## Features

- 📻 **Podcast-Bibliothek** — RSS-Feeds abonnieren, OPML-Import, Auto-Transkription
- 🎙️ **Transkription** — Gemini 1.5 Flash *oder* lokales Whisper, Sprecher-Erkennung, Zeitstempel, Kapitel
- 🎧 **Audio-Player mit synchronem Transkript** — Mitlesen während des Hörens, Klick auf Absatz springt zur Stelle, Auto-Scroll, Geschwindigkeit
- 🤖 **KI-Features** — Zusammenfassung, Key Takeaways, klickbare Kapitel-Navigation
- 🔍 **Volltextsuche** — Über alle Transkripte (SQLite FTS5)
- 📰 **Zeitung** — Journalistische Artikel aus Transkripten (Gemini 1.5 Pro)
- 📲 **PWA + Offline** — Als App installierbar, Service Worker, Push via ntfy.sh
- 📦 **Export** — TXT, Markdown, AI-optimiertes Format, Bulk-Export
- ⭐ **Leseliste** — Ungelesen-Tracking, Scroll-Position, Notizen, Schriftgröße
- 🇩🇪 **Übersetzung** — Deutsche Übersetzung auf Anfrage
- 📱 **Mobile-first** — Bottom-Navigation, Dark Mode, Touch-optimiert

## Schnellstart

```bash
cp .env.example .env
# GEMINI_API_KEY in .env eintragen
docker-compose up -d --build
# → http://localhost:7878
```

Gemini API Key: https://aistudio.google.com/apikey

## Deployment

Siehe [DEPLOY.md](DEPLOY.md) für die vollständige Anleitung (Synology DS218+, Handy-PWA, ntfy.sh).

## Tech Stack

Python · FastAPI · SQLite · yt-dlp · Gemini 1.5 · Docker
