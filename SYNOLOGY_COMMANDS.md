# Synology Befehlsreferenz — PodScribe

> Kurzreferenz der wichtigsten SSH-Befehle für die Synology DS218+  
> © 2026 Sven Kompe

---

## 🔑 SSH-Verbindung

```bash
# Von Windows (PowerShell oder Terminal):
ssh nevsepmok@192.168.178.21

# Passwort-Eingabe: Tippen ist unsichtbar — einfach eintippen + Enter
```

---

## 📁 Navigation & Dateien

```bash
# Ins Projektverzeichnis wechseln
cd /volume1/docker/Podcast_transcriptor

# Aktuelles Verzeichnis anzeigen
pwd

# Dateien auflisten
ls -la

# Datei ausgeben
cat .env

# Datei mit vi bearbeiten
vi .env
# → i = Insert-Modus (tippen)
# → Esc = Bearbeitungsmodus verlassen
# → :wq + Enter = Speichern & beenden
# → :q! + Enter = Beenden ohne Speichern

# Datei mit nano bearbeiten (falls PATH gesetzt)
/opt/bin/nano .env
# → Strg+O = Speichern
# → Strg+X = Beenden

# Verzeichnisse anlegen
mkdir -p downloads data

# Datei löschen
rm dateiname

# Verzeichnis löschen
rm -rf verzeichnisname
```

---

## 📝 .env Datei verwalten

```bash
# .env neu schreiben (EINZELN eingeben, nicht mit anderen Zeilen!)
printf 'GEMINI_API_KEY=AIzaDEINKEY\n' > .env

# .env anzeigen / prüfen
cat .env

# .env mit Editor bearbeiten
vi .env
```

---

## 🐳 Docker

```bash
# Container starten (im Hintergrund)
sudo docker-compose up -d

# Container starten + Image neu bauen (nach Code-Updates)
sudo docker-compose up -d --build

# Container stoppen
sudo docker-compose down

# Container neu starten (z.B. nach .env-Änderung)
sudo docker-compose restart

# Status aller laufenden Container
sudo docker ps

# Logs eines Containers anzeigen
sudo docker logs podscribe

# Logs live mitlesen (Strg+C zum Beenden)
sudo docker logs -f podscribe

# Logs — nur die letzten 50 Zeilen
sudo docker logs podscribe --tail 50

# In einen laufenden Container einloggen
sudo docker exec -it podscribe /bin/bash
```

---

## 📦 Projekt aktualisieren (Update)

```bash
cd /volume1/docker/Podcast_transcriptor

# Datenbank sichern (empfohlen vor Updates)
sudo cp data/podscribe.db data/podscribe.db.backup

# Neue Version herunterladen
wget https://github.com/publicnevs/Podcast_transcriptor/archive/refs/heads/main.tar.gz

# Entpacken (überschreibt bestehende Dateien — .env und data/ bleiben unangetastet)
tar -xzf main.tar.gz --strip-components=1

# Archiv aufräumen
rm main.tar.gz

# Container neu bauen & starten
sudo docker-compose up -d --build
```

> **Nach jedem Update:** im Browser einmal **hart neu laden** (`Strg+F5`) bzw. die PWA neu
> öffnen. Sonst zeigt der Service-Worker evtl. noch die alte Version (Cache wird bei
> Shell-Änderungen über einen neuen Cache-Namen, z. B. `podscribe-v3`, automatisch ersetzt).

---

## 🌐 Netzwerk

```bash
# IP-Adresse der Synology anzeigen
ip addr show | grep "inet "
# → Heimnetz-IP: 192.168.178.21 (auf ovs_eth0)

# Verbindung zum Container testen
curl http://localhost:7878/health
```

---

## 🔧 Entware (Paketmanager)

```bash
# Pakete aktualisieren
sudo opkg update

# Paket installieren
sudo opkg install git nano htop

# Installierte Pakete anzeigen
sudo opkg list-installed

# PATH für aktuelle Session setzen (falls Befehle nicht gefunden)
export PATH=/opt/bin:/opt/sbin:$PATH
```

---

## 💡 Tipps & Stolperfallen

| Problem | Lösung |
|---|---|
| `command not found` bei nano/git | `export PATH=/opt/bin:/opt/sbin:$PATH` eingeben |
| Prompt-Chaos beim Copy/Paste | **Immer nur eine Zeile** kopieren und Enter abwarten |
| Passwort-Eingabe zeigt nichts | Normal — einfach tippen + Enter |
| `.env` hat falsches Format | `cat .env` prüfen — muss `KEY=wert` sein, nicht nur den Wert |
| Container startet nicht | `sudo docker logs podscribe --tail 30` für Fehlerdetails |
| Bind mount failed | `mkdir -p downloads data` im Projektordner ausführen |
| `hostname -I` funktioniert nicht | `ip addr show \| grep "inet "` nutzen |

---

## 🌍 PodScribe URLs

| Umgebung | URL |
|---|---|
| Heimnetz (PC/Handy im WLAN) | `http://192.168.178.21:7878` |
| Direkt auf der Synology | `http://localhost:7878` |
| Features & Info | `http://192.168.178.21:7878/about` |
| Einstellungen | `http://192.168.178.21:7878/settings` |

---

*Synology DS218+ · User: nevsepmok · Projektpfad: `/volume1/docker/Podcast_transcriptor`*
