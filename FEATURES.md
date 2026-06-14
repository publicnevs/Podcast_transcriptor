# PodScribe — Features aus Endanwendersicht

> **Was ist PodScribe?**
> Dein privater, selbst-gehosteter Podcast-Hub. PodScribe lädt Podcast-Folgen,
> transkribiert sie automatisch per KI, fasst sie zusammen, macht alles durchsuchbar
> und liefert dir einen synchronen Audio-/Lese-Player — als App auf dem Handy oder im
> Browser. Dazu kommen KI-Tools wie eine „Frag-deine-Bibliothek"-Suche und eine
> automatisch generierte Redaktion (Zeitung).

Dieses Dokument beschreibt **alles, was die App kann**, in Alltagssprache. Es ist die
verbindliche Feature-Liste — bei jeder neuen oder geänderten Funktion wird sie aktualisiert.

---

## 📡 Podcasts abonnieren & verwalten

- **RSS-Feeds abonnieren** — per RSS-URL, YouTube-Kanal oder direkter Audio-URL.
- **Newsletter-Abos** — E-Mail-Newsletter landen als „Podcast" in der Bibliothek (per IMAP-Postfach); jeder Absender wird zu einem eigenen Feed gruppiert.
- **Websites abonnieren & aufbereiten** — eine beliebige Webseite eingeben: entweder **einmalig aufbereiten** (Text wird gescraped und als Artikel mit Zusammenfassung abgelegt) oder **dauerhaft überwachen** (PodScribe prüft die Seite regelmäßig und legt bei Änderungen automatisch einen neuen Eintrag an).
- **OPML-Import** — alle Abos auf einmal aus Apple Podcasts, Pocket Casts & Co. übernehmen.
- **Automatische Folgen-Erkennung** — neue Folgen werden stündlich im Hintergrund gefunden.
- **Auto-Transkription pro Podcast** — pro Feed festlegen, ob neue Folgen automatisch transkribiert werden.
- **Flexible Abo-Optionen** — maximale Folgenzahl begrenzen, Prüf-Intervall einstellen, gezielt einzelne Folgen transkribieren.
- **„Neue Folgen suchen"-Button** — einen Feed jederzeit manuell auf neue Folgen prüfen.
- **Newsletter-Avatare** — Feeds ohne Bild bekommen ein automatisch generiertes Icon (Farbe + Initialen + Mail-Symbol).

## 🗂️ Kategorien & Startseite

- **Eigene Kategorien** — lege beliebige Kategorien an (z.B. Gesundheit, Finanzen, News, KI), benenne sie um oder lösche sie (in den Einstellungen).
- **Podcasts zuordnen** — jedem Feed in seinen Einstellungen eine Kategorie zuweisen.
- **Startseite nach Kategorien** — die Bibliothek ist in Kategorie-Abschnitte gegliedert; jede Überschrift führt zu einer Kategorie-Übersicht (Quellen, Themen-Tags und neueste Folgen gebündelt).
- **Per Drag-&-Drop sortieren** — Kacheln auf der Startseite ziehen, um die Reihenfolge zu ändern oder sie in eine andere Kategorie zu verschieben.

## 📥 Neuzugänge

- **Neuzugänge-Seite** — alle frisch eingetroffenen, noch nicht aufbereiteten Folgen, Artikel und Mails an einem Ort.
- **Transkribieren auf Knopfdruck** — einzelne Neuzugänge gezielt transkribieren/aufbereiten lassen.
- **Sofort aktualisieren** — „Auf neue Folgen prüfen" (alle Feeds) und „Postfach prüfen" (Newsletter) direkt von der Seite auslösen.

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
- **Artikel in der Redaktion erstellen** — direkt aus einer Folge (von der Zusammenfassung aus) einen journalistischen Artikel aus dem Transkript generieren lassen.

## ▶️ Audio-Player mit synchronem Mitlesen

- **Synchrones Mitlesen** — der aktuelle Absatz wird hervorgehoben und scrollt automatisch mit.
- **Klick-to-seek** — Klick auf einen Absatz → Audio springt an die Stelle.
- **Geschwindigkeit** — 1× bis 2× für effizientes Nachhören.
- **15-Sekunden-Sprung** — schnell vor- und zurückspringen.
- **Deep-Links** — Links der Form `…/episode/123?t=00:05:30` öffnen die Folge und springen direkt zur Minute.

## 🔎 Bibliothek, Suche & Intelligenz

- **Volltextsuche** — blitzschnelle Suche über alle Transkripte (auch bei großen Beständen).
- **„Frag deine Bibliothek" (KI-Chat)** — stelle Fragen in natürlicher Sprache; die KI antwortet auf Basis deiner Folgen **mit zitierten Quellen** und Deep-Links zur genauen Stelle. Mehrere Folgefragen im Gespräch möglich. Das Gespräch lässt sich **als Markdown exportieren oder drucken** (inkl. Quellen).
- **Verwandte Folgen** — zu jeder Folge passende andere Folgen (über gemeinsame Themen & inhaltliche Ähnlichkeit).
- **Themen-Explorer** — pro Schlagwort eine chronologische Zeitleiste aller Folgen plus optionale themenübergreifende Zusammenfassung.
- **Ungelesen-Tracking** — Badge pro Podcast, gespeicherte Lese-/Scroll-Position, „als gelesen" markieren.
- **Takeaway-Ticker** — auf der Startseite eine wischbare Reihe der neuesten Erkenntnisse.
- **Filter & Sortierung** — nach Podcast, Zeitraum, Lese- und Transkriptions-Status.
- **Notizen** — persönliche Notizen pro Folge.

## 📰 PodScribe Redaktion (Digest)

- **Vier Ressorts:**
  - **Überblick** — kompaktes Briefing der letzten Folgen.
  - **Magazin** — ausführlicher Artikel mit einstellbarer Länge.
  - **Dossier** — tiefe Themen-Recherche zu einem gesetzten Schwerpunkt.
  - **Freier Artikel** — schreibe eine Zeitung/einen Artikel aus einer **freien Anweisung (Prompt)**. Wahlweise wählt die **KI passende Folgen** aus deiner Bibliothek selbst aus, oder du **wählst sie manuell**. Auch ganz ohne Quellen (reiner Prompt) möglich.
- **Live-Vorschau** — vor dem Erzeugen sehen, welche Folgen einfließen und wie lange das Lesen dauert.
- **Journalistische Artikel** — echte Redaktionstexte (1200–2000 Wörter), KI-geschrieben aus deinen Transkripten.
- **TL;DR oben** — jeder Artikel startet mit dem Wichtigsten in Kürze.
- **Rezepte & Zeitplan** — wiederkehrende Ausgaben speichern und automatisch zu festen Zeiten erzeugen lassen.
- **Automatische Trending-Ausgabe** — wöchentlich automatisch eine Ausgabe aus den aktuell meistdiskutierten Themen (an-/abschaltbar, Wochentag & Uhrzeit wählbar).
- **Per E-Mail zustellen** — Ausgaben automatisch oder manuell per Mail verschicken.
- **Teilen** — Ausgabe über einen Link teilen.

## 📤 Export

- **Einzel-Export** — Folge als TXT, Markdown oder PDF herunterladen.
- **Bulk-Export** — alle Folgen eines Podcasts als eine große Markdown-Datei (ideal für KI-Recherche).
- **Drucken/PDF** — saubere Druckansicht direkt aus dem Browser.

## 🔐 Zugriff & Rollen

- **Standardmäßig offen** — solange kein Eigentümer-Passwort gesetzt ist, funktioniert alles wie gewohnt (Einzelnutzer).
- **Eigentümer-Login** — mit gesetztem Passwort hat nur der Eigentümer Vollzugriff (transkribieren, Einstellungen, Redaktion/Ausgaben erstellen usw.).
- **Gast-/Lesezugriff** — Gäste können die Bibliothek, Folgen und die Suche lesen, aber nichts ändern oder kostenpflichtige KI-Aktionen auslösen.
- **Gast-KI optional** — KI-Chat für Gäste freischaltbar, mit Limit pro Stunde zum Kostenschutz.

## 📱 App & Mobile (PWA)

- **Als App installierbar** — auf den Homescreen (Android/iOS), ohne App Store.
- **Offline-Lesen** — bereits geladene Transkripte sind auch ohne Internet verfügbar.
- **Mobile-first** — Bottom-Navigation, große Touch-Flächen, Schriftgrößen-Regler.
- **„Mehr"-Menü** — über die Bottom-Navigation sind auch am Handy (und für Gäste) Fragen, Radar, Tags, Über und der **Design-Umschalter** erreichbar.
- **Helles & dunkles Design** — die App startet im **hellen Design**; per Umschalter (auch für Gäste, am PC oben und mobil im „Mehr"-Menü) jederzeit auf dunkel wechselbar, die Wahl bleibt gespeichert.
- **Wischbare Highlights** — der News-Streifen auf der Startseite lässt sich am Handy wischen und am PC per Maus ziehen/scrollen.
- **Push-Benachrichtigungen** — via ntfy.sh aufs Handy, sobald ein Transkript fertig ist (mit tippbarem Direktlink).

---

*PodScribe · © 2026 Sven Kompe · Self-Hosted auf Synology · entwickelt mit Claude.*
