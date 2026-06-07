# 🎙️ PodScribe — Privater Podcast-Transkriptions-Hub

> **© 2026 Sven Kompe** · Self-Hosted · Open Source  
> Entwickelt mit ♥ und Claude · Deployment auf Synology DS218+

PodScribe ist eine vollständige Self-Hosting-Lösung für Podcast-Transkription. Du abonnierst deine Podcasts einmal — PodScribe prüft automatisch auf neue Folgen, lädt sie herunter, transkribiert sie mit Gemini AI und gibt dir eine durchsuchbare Bibliothek mit Zusammenfassungen, Kapiteln und Audio-Player mit synchronem Mitlesen.

**Live unter:** `http://192.168.178.21:7878` (Heimnetz)  
**GitHub:** [github.com/publicnevs/Podcast_transcriptor](https://github.com/publicnevs/Podcast_transcriptor)

---

## ✨ Features

### 📻 Podcast-Management
- RSS-Feed, YouTube-Kanal oder direkte Audio-URL abonnieren
- **OPML-Import** — alle Podcasts aus Apple Podcasts / Pocket Casts auf einmal importieren
- Auto-Transkription neuer Folgen (konfigurierbar pro Podcast)
- Max. Folgen limitieren, Check-Intervall konfigurieren

### 🎙️ Transkription (umschaltbares Backend)
| Backend | Qualität | Geschwindigkeit | Kosten |
|---|---|---|---|
| **Gemini 1.5 Flash** (empfohlen) | ⭐⭐⭐⭐⭐ | Sehr schnell | Free-Tier ~15 Folgen/Tag |
| **Whisper local** (optional) | ⭐⭐⭐ | Langsam auf DS218+ | Kostenlos, offline |

Whisper ist als optionaler Docker-Build-Arg verfügbar (`INSTALL_WHISPER=true`). Auf der DS218+ (Intel Celeron J3355, kein AVX2) empfiehlt sich Modell `base` — eine 60-Min-Folge dauert ca. 2-3 Stunden.

Beide Backends erzeugen:
- Vollständiges Transkript mit **Zeitstempeln**
- **Sprecher-Erkennung** (Host/Gast farblich markiert)
- **Zusammenfassung** (5 Sätze)
- **Key Takeaways** (6-10 Bullet Points)
- **Kapitel** mit klickbaren Zeitstempeln

### 🎧 Audio-Player mit synchronem Transkript
- **Mitlesen beim Hören** — aktiver Absatz wird hervorgehoben & scrollt mit
- **Klick auf Absatz** → Audio springt direkt zur Stelle
- Geschwindigkeit: 1× / 1.25× / 1.5× / 1.75× / 2×
- 15-Sekunden-Sprung vor/zurück
- Range-Support für korrektes Seeking
- Auto-Scroll on/off schaltbar

### 🤖 KI-Features (alle via Gemini)
- **Auto-Zusammenfassung** nach jeder Transkription
- **Key Takeaways** — die wichtigsten Erkenntnisse auf einen Blick
- **Automatische Kapitel** mit Zeitstempeln (klickbare Navigation)
- **Deutsche Übersetzung** on demand (kein Auto-Übersetzen — spart Kosten)
- **Für KI kopieren** — Transkript + Metadaten optimal formatiert für Claude / Gemini

### 🔍 Bibliothek & Suche
- **Volltextsuche** über alle Transkripte (SQLite FTS5, blitzschnell)
- Filter nach Podcast, Zeitraum, Lesestatus, Transkriptions-Status
- **Ungelesen-Tracking** — Badge pro Podcast, Scroll-Position gespeichert
- **Leseliste** — Folgen für später vormerken
- Persönliche **Notizen** pro Episode

### 📦 Export
- Einzelne Episode: **TXT / Markdown / PDF**
- **Bulk-Export** — alle Folgen eines Podcasts als eine Markdown-Datei (ideal für AI-Research: "Analysiere alle 50 Folgen und erstelle eine Wissensdatenbank")
- Drucken/PDF direkt aus dem Browser

### 📰 PodScribe Zeitung (Digest)
- Gemini 1.5 Pro generiert **journalistische Artikel** aus Transkripten (kein Stichpunkt-Summary, echter Redaktionstext, 1200-2000 Wörter)
- **Wochenzeitung** — alle Folgen der letzten 7 Tage
- **Themen-Report** — manuell ausgewählte Folgen kombinieren
- **Podcast-Portrait** — alle Folgen eines Casts analysieren
- Markdown-Export des generierten Artikels

### 📲 PWA & Mobile
- Als **echte App** auf dem Homescreen installierbar (Android/iOS)
- **Service Worker** — Transkripte offline lesbar (auch im Zug)
- **Mobile-first Design** — Bottom-Navigation, große Touch-Targets, Dark Mode
- **Reading Progress Bar**, Schriftgrößen-Regler im Reader
- **Push-Benachrichtigungen** via ntfy.sh — Nachricht wenn Transkript fertig

### 🔔 Automatisierung
- **Hintergrund-Scheduler** (APScheduler) — prüft stündlich/täglich auf neue Folgen
- Live-Status in der UI: Warte → Lädt herunter → Transkribiert → Fertig / Fehler
- Manueller Trigger über API oder UI ("Jetzt prüfen")

---

## 🏗️ Architektur

```
Podcast_transcriptor/
├── app/
│   ├── main.py              # FastAPI-App, alle API-Routen, Audio-Proxy
│   ├── scheduler.py         # APScheduler: neue Folgen prüfen + auto-transkribieren
│   ├── downloader.py        # yt-dlp: URL → MP3/Audio-Datei
│   ├── transcriber.py       # Gemini: Audio → Transkript + Summary + Kapitel
│   ├── whisper_backend.py   # Optionales lokales Whisper-Backend (faster-whisper)
│   ├── feed_parser.py       # RSS + OPML parsen, Spotify-Redirect
│   ├── processor.py         # Orchestrierung: Download → Transkription → DB-Update
│   ├── exporter.py          # TXT / Markdown / PDF-Export
│   ├── notifier.py          # ntfy.sh Push-Benachrichtigungen
│   ├── database.py          # SQLite-Setup, FTS5-Index, alle DB-Operationen
│   └── static/
│       ├── index.html       # Bibliothek: Podcasts, Suche, Filter
│       ├── podcast.html     # Folgen-Liste eines Podcasts
│       ├── episode.html     # Transkript + Player + Kapitel + Notizen
│       ├── digest.html      # Zeitung / Digest-Modus
│       ├── settings.html    # API-Key, Backend, ntfy, Intervalle
│       ├── about.html       # Feature-Übersicht + Copyright
│       ├── app.js           # Shared JS: Nav, AudioPlayer, SW-Registration, Helpers
│       ├── style.css        # Design-System: Tokens, Dark Mode, Mobile, Player
│       ├── sw.js            # Service Worker (PWA + Offline-Caching)
│       ├── manifest.json    # PWA-Manifest
│       └── icon.svg         # App-Icon
├── data/
│   └── podscribe.db         # SQLite-Datenbank (persistiert via Docker-Volume)
├── downloads/               # Temporäre Audiodateien (nach Transkription gelöscht)
├── requirements.txt         # Python-Dependencies (Basis)
├── requirements-whisper.txt # Optional: faster-whisper für lokales Backend
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── DEPLOY.md                # Deployment-Anleitung (Synology)
└── README.md                # Diese Datei
```

---

## 🗄️ Datenbankschema (SQLite)

```sql
podcasts (
  id, title, rss_url, artwork_url, description, website_url, language,
  auto_transcribe, check_interval_hours, last_checked, max_episodes, created_at
)

episodes (
  id, podcast_id, title, audio_url, pub_date, duration_sec, guid,
  status,           -- pending | queued | downloading | transcribing | done | error
  error_msg, read_at, watchlist, scroll_pos, notes, created_at
)

transcripts (
  id, episode_id, content, language, word_count, model_used,
  segments_json,    -- JSON-Array mit {time, speaker, text} für Player-Sync
  translation,      -- Deutsche Übersetzung (on demand)
  created_at
)

summaries (
  id, episode_id, summary, takeaways_json, chapters_json, created_at
)

digests (
  id, title, subtitle, content_md, reading_time_min, episode_ids_json,
  mode, created_at
)

settings (key, value)             -- Key-Value-Store für App-Einstellungen

-- FTS5 Virtual Table (Volltextsuche)
transcripts_fts (content)
```

---

## 🔌 API-Routen

```
# Podcasts
POST   /api/podcasts              — Podcast abonnieren (RSS-URL)
POST   /api/podcasts/opml         — OPML-Import (multipart)
GET    /api/podcasts              — Alle Podcasts + Counts
DELETE /api/podcasts/{id}
GET    /api/podcasts/{id}/episodes
GET    /api/podcasts/{id}/export  — Bulk-Export als Markdown

# Episoden
GET    /api/episodes/{id}
GET    /api/episodes/{id}/export?format=md|txt|pdf
GET    /api/episodes/{id}/audio   — Audio-Proxy mit Range-Support (für Player)
PATCH  /api/episodes/{id}/read
PATCH  /api/episodes/{id}/watchlist
POST   /api/episodes/{id}/notes
POST   /api/episodes/{id}/transcribe    — Manuell transkribieren
POST   /api/episodes/{id}/translate     — Deutsche Übersetzung anfordern
POST   /api/episodes/{id}/retranscribe  — Erneut transkribieren

# Batch-Transkription (direkte URLs)
POST   /api/transcribe/batch      — { "urls": ["url1", ...] }

# Suche
GET    /api/search?q=...          — FTS5-Volltextsuche

# Queue & Scheduler
GET    /api/queue
POST   /api/scheduler/trigger

# Digest / Zeitung
GET    /api/digests
POST   /api/digests               — Artikel generieren
GET    /api/digests/{id}
DELETE /api/digests/{id}
GET    /api/digests/{id}/export

# Einstellungen
GET    /api/settings
PUT    /api/settings              — API-Key, Backend, ntfy, Intervall

# System
GET    /health
```

---

## 🚀 Deployment

### Voraussetzungen
- Docker + Docker Compose auf dem Synology NAS
- Gemini API Key von [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (beginnt mit `AIza...`)

### Schnellstart (Synology SSH)
```bash
cd /volume1/docker
wget https://github.com/publicnevs/Podcast_transcriptor/archive/refs/heads/main.tar.gz
tar -xzf main.tar.gz && mv Podcast_transcriptor-main Podcast_transcriptor
cd Podcast_transcriptor
mkdir -p data downloads
echo "GEMINI_API_KEY=AIzaDEINKEY" > .env
sudo docker-compose up -d
```

Dann im Browser: **`http://SYNOLOGY-IP:7878`**

### Mit lokalem Whisper (optional)
```bash
sudo docker-compose build --build-arg INSTALL_WHISPER=true
sudo docker-compose up -d
```
In der App unter **Einstellungen → Transkriptions-Engine** auf "Whisper (lokal)" umstellen.

### Updates einspielen
```bash
cd /volume1/docker/Podcast_transcriptor
wget -O update.tar.gz https://github.com/publicnevs/Podcast_transcriptor/archive/refs/heads/main.tar.gz
tar -xzf update.tar.gz --strip-components=1
sudo docker-compose up -d --build
```

Ausführliche Anleitung inkl. Reverse Proxy, QuickConnect und ntfy: **[DEPLOY.md](DEPLOY.md)**

---

## ⚙️ Konfiguration (.env)

```env
# Gemini API Key (https://aistudio.google.com/apikey)
GEMINI_API_KEY=AIza...

# Transkriptions-Backend: "gemini" (Standard) oder "whisper" (lokal)
TRANSCRIPTION_BACKEND=gemini

# Whisper-Modell (nur wenn Backend=whisper): tiny | base | small
WHISPER_MODEL=base

# Pfade (Defaults passen für Docker)
DB_PATH=/app/data/podscribe.db
DOWNLOAD_DIR=/app/downloads
```

Alle Einstellungen können auch **in der App unter Einstellungen** geändert werden — kein SSH nötig.

---

## 🛠️ Tech-Stack

| Bereich | Technologie |
|---|---|
| Backend | Python 3.11 + FastAPI + uvicorn |
| Datenbank | SQLite + FTS5 (Volltextsuche eingebaut) |
| Audio-Download | yt-dlp + ffmpeg |
| Transkription (Cloud) | Google Gemini 1.5 Flash |
| Transkription (lokal) | faster-whisper (CTranslate2, int8) |
| KI-Anreicherung | Google Gemini 1.5 Flash (Summary/Kapitel) |
| KI-Artikel | Google Gemini 1.5 Pro (Digest-Modus) |
| RSS/OPML-Parsing | feedparser |
| Hintergrund-Jobs | APScheduler 3.x |
| HTTP-Client | httpx (async, mit Range-Proxy) |
| Push-Benachrichtigungen | ntfy.sh |
| Frontend | Vanilla JavaScript (kein Framework, kein Build-Tool) |
| CSS | Custom Design-System (Dark Mode, CSS-Variables) |
| PWA | Service Worker + Web App Manifest |
| Schriften | Inter (Google Fonts) |
| Deployment | Docker Compose |
| Hosting | Synology DS218+ (Intel Celeron J3355, 16 GB RAM) |

---

## 📄 Lizenz & Copyright

```
Copyright © 2026 Sven Kompe
Alle Rechte vorbehalten.

Dieses Projekt ist ein privates Self-Hosting-Projekt.
```

---

*Entwickelt mit ♥ und [Claude](https://claude.ai) · [PodScribe auf GitHub](https://github.com/publicnevs/Podcast_transcriptor)*
