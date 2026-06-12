# PodScribe — Features aus Endanwendersicht

> **Was ist PodScribe?**
> Dein privater, selbst-gehosteter Podcast-Hub. PodScribe lädt Podcast-Folgen,
> transkribiert sie automatisch per KI, fasst sie zusammen, macht alles durchsuchbar
> und liefert dir einen synchronen Audio-/Lese-Player — als App auf dem Handy oder im
> Browser. Dazu kommen KI-Tools wie eine „Frag-deine-Bibliothek"-Suche und eine
> automatisch generierte Zeitung.

Dieses Dokument beschreibt **alles, was die App kann**, in Alltagssprache. Es ist die
verbindliche Feature-Liste — bei jeder neuen oder geänderten Funktion wird sie aktualisiert.

---

## 📡 Podcasts abonnieren & verwalten

- **RSS-Feeds abonnieren** — per RSS-URL, YouTube-Kanal oder direkter Audio-URL.
- **Newsletter-Abos** — E-Mail-Newsletter landen als „Podcast" in der Bibliothek (per IMAP-Postfach); jeder Absender wird zu einem eigenen Feed gruppiert.
- **OPML-Import** — alle Abos auf einmal aus Apple Podcasts, Pocket Casts & Co. übernehmen.
- **Automatische Folgen-Erkennung** — neue Folgen werden stündlich im Hintergrund gefunden.
- **Auto-Transkription pro Podcast** — pro Feed festlegen, ob neue Folgen automatisch transkribiert werden.
- **Flexible Abo-Optionen** — maximale Folgenzahl begrenzen, Prüf-Intervall einstellen, gezielt einzelne Folgen transkribieren.
- **„Neue Folgen suchen"-Button** — einen Feed jederzeit manuell auf neue Folgen prüfen.
- **Newsletter-Avatare** — Feeds ohne Bild bekommen ein automatisch generiertes Icon (Farbe + Initialen + Mail-Symbol).

## 🎙️ Transkription

- **Gemini 2.5 Flash (Cloud)** — schnelle, günstige KI-Transkription (empfohlen).
- **Lokales Whisper-Backend** — faster-whisper (tiny/base/small) für 100 % offline & kostenlos, dafür langsamer.
- **Vorhandene Transkripte nutzen** — liefert ein Feed bereits ein Transkript, wird der Audio-Download übersprungen (schneller & günstiger).
- **Sprecher-Erkennung** — Host und Gäste werden erkannt und farblich markiert.
- **Klickbare Zeitstempel** — jeder Absatz ist mit einer Stelle im Audio verknüpft.

## 🤖 KI-Aufbereitung jeder Folge

- **Auto-Zusammenfassung** — kompakte Zusammenfassung direkt nach der Transkription.
- **Key Takeaways** — die wichtigsten Erkenntnisse als Stichpunkte.
- **Themenübersicht** — worum es in der Folge geht, auf einen Blick.
- **Automatische Kapitel** — Themenblöcke mit Zeitstempeln zum Anspringen.
- **Auto-Tagging** — Folgen werden automatisch mit Themen-Schlagwörtern versehen (mit cleverer Dubletten-Vermeidung).
- **Deutsche Übersetzung** — fremdsprachige Transkripte per Klick ins Deutsche übersetzen.
- **„Für KI kopieren"** — Transkript + Metadaten optimal formatiert für Claude / Gemini / ChatGPT.
- **Zusammenfassung neu erzeugen** — auf Wunsch neu generieren lassen.

## ▶️ Audio-Player mit synchronem Mitlesen

- **Synchrones Mitlesen** — der aktuelle Absatz wird hervorgehoben und scrollt automatisch mit.
- **Klick-to-seek** — Klick auf einen Absatz → Audio springt an die Stelle.
- **Geschwindigkeit** — 1× bis 2× für effizientes Nachhören.
- **15-Sekunden-Sprung** — schnell vor- und zurückspringen.
- **Deep-Links** — Links der Form `…/episode/123?t=00:05:30` öffnen die Folge und springen direkt zur Minute.

## 🔎 Bibliothek, Suche & Intelligenz

- **Volltextsuche** — blitzschnelle Suche über alle Transkripte (auch bei großen Beständen).
- **„Frag deine Bibliothek" (KI-Chat)** — stelle Fragen in natürlicher Sprache; die KI antwortet auf Basis deiner Folgen **mit zitierten Quellen** und Deep-Links zur genauen Stelle. Mehrere Folgefragen im Gespräch möglich.
- **Verwandte Folgen** — zu jeder Folge passende andere Folgen (über gemeinsame Themen & inhaltliche Ähnlichkeit).
- **Themen-Explorer** — pro Schlagwort eine chronologische Zeitleiste aller Folgen plus optionale themenübergreifende Zusammenfassung.
- **Ungelesen-Tracking** — Badge pro Podcast, gespeicherte Lese-/Scroll-Position, „als gelesen" markieren.
- **Takeaway-Ticker** — auf der Startseite eine wischbare Reihe der neuesten Erkenntnisse.
- **Filter & Sortierung** — nach Podcast, Zeitraum, Lese- und Transkriptions-Status.
- **Notizen** — persönliche Notizen pro Folge.

## 📰 PodScribe Zeitung (Digest)

- **Drei Ressorts:**
  - **Überblick** — kompaktes Briefing der letzten Folgen.
  - **Magazin** — ausführlicher Artikel mit einstellbarer Länge.
  - **Dossier** — tiefe Themen-Recherche zu einem gesetzten Schwerpunkt.
- **Live-Vorschau** — vor dem Erzeugen sehen, welche Folgen einfließen und wie lange das Lesen dauert.
- **Journalistische Artikel** — echte Redaktionstexte (1200–2000 Wörter), KI-geschrieben aus deinen Transkripten.
- **TL;DR oben** — jeder Artikel startet mit dem Wichtigsten in Kürze.
- **Rezepte & Zeitplan** — wiederkehrende Ausgaben speichern und automatisch zu festen Zeiten erzeugen lassen.
- **Automatische Trending-Ausgabe** — wöchentlich automatisch eine Zeitung aus den aktuell meistdiskutierten Themen (an-/abschaltbar, Wochentag & Uhrzeit wählbar).
- **Per E-Mail zustellen** — Ausgaben automatisch oder manuell per Mail verschicken.
- **Teilen** — Ausgabe über einen Link teilen.

## 📤 Export

- **Einzel-Export** — Folge als TXT, Markdown oder PDF herunterladen.
- **Bulk-Export** — alle Folgen eines Podcasts als eine große Markdown-Datei (ideal für KI-Recherche).
- **Drucken/PDF** — saubere Druckansicht direkt aus dem Browser.

## 🔐 Zugriff & Rollen

- **Standardmäßig offen** — solange kein Eigentümer-Passwort gesetzt ist, funktioniert alles wie gewohnt (Einzelnutzer).
- **Eigentümer-Login** — mit gesetztem Passwort hat nur der Eigentümer Vollzugriff (transkribieren, Einstellungen, Zeitung erstellen usw.).
- **Gast-/Lesezugriff** — Gäste können die Bibliothek, Folgen und die Suche lesen, aber nichts ändern oder kostenpflichtige KI-Aktionen auslösen.
- **Gast-KI optional** — KI-Chat für Gäste freischaltbar, mit Limit pro Stunde zum Kostenschutz.

## 📱 App & Mobile (PWA)

- **Als App installierbar** — auf den Homescreen (Android/iOS), ohne App Store.
- **Offline-Lesen** — bereits geladene Transkripte sind auch ohne Internet verfügbar.
- **Mobile-first** — Bottom-Navigation, große Touch-Flächen, Schriftgrößen-Regler, Dark Mode.
- **Push-Benachrichtigungen** — via ntfy.sh aufs Handy, sobald ein Transkript fertig ist (mit tippbarem Direktlink).

---

*PodScribe · © 2026 Sven Kompe · Self-Hosted auf Synology · entwickelt mit Claude.*
