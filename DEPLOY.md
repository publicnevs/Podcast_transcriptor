# PodScribe — Deployment auf Synology DS218+

## Voraussetzungen

- Synology DSM 7.x
- Docker-Paket installiert (Package Center → Docker)
- SSH aktiviert (Systemsteuerung → Terminal & SNMP)
- Git installiert (Package Center → Git Server, oder manuell)

---

## Schritt 1 — Per SSH verbinden

```bash
ssh deinname@synology-ip
# z.B. ssh admin@192.168.1.100
```

---

## Schritt 2 — Repo klonen

```bash
cd /volume1  # oder dein bevorzugtes Verzeichnis
git clone https://github.com/publicnevs/Podcast_transcriptor
cd Podcast_transcriptor
```

---

## Schritt 3 — .env Datei anlegen

```bash
cp .env.example .env
vi .env
# oder: nano .env (falls nano installiert)
```

Inhalt anpassen:
```
GEMINI_API_KEY=AIza...deinKey...
```

Speichern: `:wq` (vi) oder `Ctrl+X, Y, Enter` (nano)

---

## Schritt 4 — Starten

```bash
docker-compose up -d --build
```

Erster Build dauert 3-5 Minuten (ffmpeg Download).

Status prüfen:
```bash
docker-compose logs -f
# Warte bis: "Application startup complete"
```

---

## Schritt 5 — Zugriff

**Im Heimnetz:**
```
http://192.168.1.100:7878
```
(IP deiner Synology anpassen)

**Von außen (Synology QuickConnect):**
1. DSM → Systemsteuerung → Anwendungsportal → Reverse Proxy
2. Neue Regel:
   - Protokoll: HTTPS
   - Hostname: `podscribe.DEIN-QUICKCONNECT.synology.me`
   - Port: 443
   - Weiterleitung: `localhost:7878`

---

## Schritt 6 — Handy einrichten (Samsung S23 Ultra)

1. Chrome öffnen → `http://192.168.1.100:7878`
2. Menü (⋮) → **"Zum Startbildschirm hinzufügen"**
3. PodScribe ist jetzt als App installiert

**Push-Benachrichtigungen:**
1. [ntfy-App](https://play.google.com/store/apps/details?id=io.heckel.ntfy) aus dem Play Store installieren
2. In der App: + → Kanal abonnieren → `podscribe-m7k4p9x2` (oder deinen Kanal-Namen)
3. In PodScribe Settings den gleichen Kanal eintragen → Test-Notification senden

---

## Updates einspielen

```bash
cd /volume1/Podcast_transcriptor
git pull
docker-compose up -d --build
```

---

## Nützliche Befehle

```bash
# Logs anzeigen
docker-compose logs -f

# Container neustarten
docker-compose restart

# Stoppen
docker-compose down

# Datenbank-Backup
cp data/podscribe.db data/podscribe_backup_$(date +%Y%m%d).db
```

---

## Troubleshooting

**Problem: "Gemini API Key ungültig"**
→ `.env` prüfen, Key von https://aistudio.google.com/apikey kopieren
→ `docker-compose restart` danach

**Problem: Download schlägt fehl**
→ ffmpeg ist im Container enthalten, sollte funktionieren
→ Prüfe ob die URL direkt erreichbar ist
→ `docker-compose logs podscribe` für Details

**Problem: Port 7878 bereits belegt**
→ In `docker-compose.yml` Port ändern, z.B. `"7979:7878"`

**Problem: Synology zu langsam für Transkription**
→ Die DS218+ transkribiert nicht selbst — Gemini läuft in der Cloud
→ Download und Upload können bei großen Dateien etwas dauern
