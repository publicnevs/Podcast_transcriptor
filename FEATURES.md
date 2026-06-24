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
- **Volltext statt nur Überschrift** — bei Artikel-Feeds, die nur einen Anriss („… weiterlesen") liefern, holt PodScribe automatisch den **vollständigen Artikeltext** von der Originalseite (sauber von Menü/Werbung befreit). Für neue Artikel-Feeds standardmäßig aktiv, pro Feed abschaltbar.
- **OPML-Import mit Massen-Abo** — alle Abos aus Apple Podcasts, Pocket Casts & Co. auf einmal übernehmen: eine Datei wählen, „Alle abonnieren" — PodScribe legt jeden Feed an und zeigt, welche geklappt haben und welche nicht.
- **Quellen-Typ pro Feed umstellbar** — falsch erkannt? In den Feed-Einstellungen zwischen **Podcast (Audio)**, **Newsfeed (Artikel/Text)** und **Website** wechseln. Gemischte Feeds (Audio + Artikel) werten Artikel automatisch als Text aus, statt sie fälschlich als Audio zu laden.
- **Automatische Folgen-Erkennung** — neue Folgen werden stündlich im Hintergrund gefunden; wiederholt nicht erreichbare Feeds werden seltener (mit wachsendem Abstand) geprüft, statt jede Stunde erneut zu scheitern.
- **Auto-Transkription pro Podcast** — pro Feed festlegen, ob neue Folgen automatisch transkribiert werden. **Schutz vor Massen-Transkription:** ein neu abonnierter Podcast mit Auto-Transkription transkribiert zunächst **maximal 5 Folgen** (später pro Feed erhöhbar).
- **Flexible Abo-Optionen** — maximale Folgenzahl begrenzen, Prüf-Intervall einstellen, gezielt einzelne Folgen transkribieren.
- **Sichere URL-Eingabe** — beim Abonnieren/Scrapen werden nur echte Web-Adressen akzeptiert (kein localhost/interne Adressen).
- **Max. Audio-Länge** — sehr lange Folgen werden mit klarer Meldung übersprungen (Limit in den Einstellungen), statt erst spät am Größenlimit zu scheitern.
- **„Neue Folgen suchen"-Button** — einen Feed jederzeit manuell auf neue Folgen prüfen.
- **Echte Logos für Newsletter & Websites** — PodScribe holt automatisch das Logo der Quelle (og:image bzw. Favicon der Absender-/Webseite, oder ein Logo aus der Newsletter-Mail). Klappt das nicht, gibt es weiterhin ein generiertes Icon (Farbe + Initialen, bei Newslettern mit Mail-Symbol). Das Logo lässt sich pro Quelle auch **manuell überschreiben** (Logo-URL in den Feed-Einstellungen).

## 🗂️ Kategorien & Startseite

- **Eigene Kategorien** — lege beliebige Kategorien an (z.B. Gesundheit, Finanzen, News, KI), **benenne sie um** (Name antippen) und **sortiere sie** per ▲▼-Pfeilen; die Reihenfolge bestimmt die Anordnung auf der Startseite. (Vorangelegt: Automotive, Sport.)
- **Podcasts zuordnen** — jedem Feed in seinen Einstellungen eine Kategorie zuweisen.
- **Startseite nach Kategorien** — die Bibliothek ist in Kategorie-Abschnitte gegliedert; jede Überschrift führt zu einer Kategorie-Übersicht (Quellen, Themen-Tags und neueste Folgen gebündelt).
- **Per Drag-&-Drop sortieren** — Kacheln auf der Startseite ziehen, um die Reihenfolge zu ändern oder sie in eine andere Kategorie zu verschieben.

## 📥 Neuzugänge

- **Zwei Abschnitte** — oben **„Neu & fertig"** (neue, bereits transkribierte Folgen/News mit Text, die du sofort lesen/hören kannst), darunter **„In Arbeit / Fehler"** (was gerade verarbeitet wird oder fehlgeschlagen ist).
- **Fehlerdetails auf Klick** — bei einem fehlgeschlagenen Eintrag die volle Fehlermeldung anzeigen und direkt „Erneut versuchen".
- **Transkribieren auf Knopfdruck** — einzelne Neuzugänge gezielt transkribieren/aufbereiten lassen.
- **Sofort aktualisieren** — „Auf neue Folgen prüfen" (alle Feeds) und „Postfach prüfen" (Newsletter) direkt von der Seite auslösen.
- **Nur für Eigentümer** — die Neuzugänge-Seite (Verarbeitung/Betrieb) ist im Gast-/Lesezugang ausgeblendet.

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
- **Aktuelles-Ticker** — auf der Startseite ein **automatisch laufender Nachrichten-Ticker** der neuesten Beiträge, jeweils mit **Quelle und Datum** (heute/gestern/Datum). Er läuft endlos durch (Marquee), pausiert beim Drüberfahren (Desktop) bzw. Antippen (Handy); ein Klick auf die Kachel öffnet die Folge bzw. den Artikel, ein Klick auf den **Quellennamen** springt zur Podcast-/Feed-Detailseite. Bei aktivierter Systemeinstellung „Bewegung reduzieren" wird stattdessen manuell gewischt.
- **Zeitungs-Schaltflächen** — direkt darüber prominente Buttons für deine **Zeitungen/Editionen** (z.B. Tageszeitung Tech, Sport, Braunschweig, Wochenmagazin); ein Klick öffnet jeweils die aktuellste Ausgabe.
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
- **Zeitungen / Editionen (automatisch)** — lege beliebig viele eigene Zeitungen an. Jede **Edition** erzeugt automatisch einen KI-Artikel: **täglich** aus den am Vortag verarbeiteten Beiträgen oder **wöchentlich** (Wochenmagazin) aus der vergangenen Woche. Pro Edition wählst du **Name, Typ (täglich/wöchentlich), Kategorien & Quellen, Uhrzeit (wöchentlich auch Wochentag), Länge/Stil** und optionalen Fokus. Ausgaben erscheinen als Schaltfläche auf der Startseite und in der Redaktion und können per E-Mail verschickt werden. „Jetzt erzeugen" baut sie sofort. Vorbefüllt mit *Tageszeitung Tech/Sport/Braunschweig* und *Wochenmagazin*.
- **Per E-Mail zustellen** — Ausgaben automatisch oder manuell per Mail verschicken.
- **Teilen** — Ausgabe über einen Link teilen.

## 📤 Export

- **Einzel-Export** — Folge als TXT, Markdown oder PDF herunterladen.
- **Bulk-Export** — alle Folgen eines Podcasts als eine große Markdown-Datei (ideal für KI-Recherche).
- **Drucken/PDF** — saubere Druckansicht direkt aus dem Browser.

## 🔐 Zugriff & Rollen

- **Standardmäßig offen** — solange kein Eigentümer-Passwort gesetzt ist, funktioniert alles wie gewohnt (Einzelnutzer).
- **Eigentümer-Login** — mit gesetztem Passwort hat nur der Eigentümer Vollzugriff (transkribieren, Einstellungen, Redaktion/Ausgaben erstellen usw.).
- **Freunde-Logins (bis zu 50)** — lege in den Einstellungen Freunde mit eigenem **Namen + Passwort** an (reiner Lesezugriff), einzeln wieder löschbar. Anmeldung auf der Login-Seite mit Name + Passwort.
- **Zugriffsmodus wählbar** — „Nur angemeldete Nutzer" (Eigentümer + Freunde, kein anonymer Zugriff — ideal, wenn die App im Internet steht), „Offen lesbar" oder „Gemeinsames Gast-Passwort".
- **Gast-/Lesezugriff** — Gäste/Freunde können die Bibliothek, Folgen und die Suche lesen, aber nichts ändern oder kostenpflichtige KI-Aktionen auslösen.
- **Gast-KI optional** — KI-Chat für Gäste freischaltbar, mit Limit pro Stunde zum Kostenschutz.
- **Nutzungsbedingungen** — eigene Seite (`/terms`): Privatnutzung & Privatkopie, keine Weitergabe ohne Urheberrechtsprüfung, kostenloses Privatprojekt.

## 📊 Statistik & Betrieb

- **Statistikseite** — eine eigene Übersichtsseite (in den Einstellungen oben und im „Mehr"-Menü verlinkt): wie viele **Quellen, Beiträge, Fertig/Ungelesen** und der **Verarbeitungsstatus** (Warteschlange, in Arbeit, Fehler) auf einen Blick.
- **Aktivität je Quelle** — pro Podcast/Feed/Webseite: wie viele **neue Beiträge** im gewählten Zeitraum, wie viele **Prüfungen (Checks)**, wann **zuletzt geprüft** — sortierbar, mit **Fehler-Warnung** bei wiederholt nicht erreichbaren Feeds. Zeitraum wählbar (7 / 30 / 90 Tage), inkl. Mini-Diagramm „neue Beiträge pro Tag".
- **Zuverlässige Warteschlange** — neue Folgen werden fortlaufend und vollständig abgearbeitet (nicht mehr nur ein paar pro Durchlauf); beim Einschalten der Auto-Transkription werden wartende Folgen sofort verarbeitet, und nach einem Neustart hängengebliebene Folgen laufen automatisch weiter.
- **Verarbeitungs-Protokoll** — auf der Statistik-Seite ein Log, was erfolgreich bzw. erfolglos geladen/transkribiert wurde (Quelle, Aktion, Detail, Zeit).
- **Feed-Gesundheit** — wiederholt fehlerhafte Feeds zeigen in den Feed-Einstellungen ein Warn-Badge mit der letzten Fehlermeldung.
- **JS-Seiten & Paywalls** — beim Scrapen wird erkannt, wenn eine Seite nur eine Anmelde-/Paywall liefert (klare Meldung statt Müll-Text). Optional kann ein Headless-Browser JavaScript-Seiten rendern (Substack/Medium/SPAs) — in den Einstellungen aktivierbar, wenn das Image entsprechend gebaut wurde.

## 📱 App & Mobile (PWA)

- **Als App installierbar** — auf den Homescreen (Android/iOS), ohne App Store.
- **Offline-Lesen** — bereits geladene Transkripte sind auch ohne Internet verfügbar.
- **Mobile-first** — Bottom-Navigation, große Touch-Flächen, Schriftgrößen-Regler.
- **„Mehr"-Menü (überall)** — am PC oben und am Handy unten ein **„Mehr"-Menü** mit den Zweitfunktionen: ganz oben **Feedback** und **Buy me a coffee**, dann Über, Radar, Tags, Statistik (Eigentümer) und der **Design-Umschalter** — auch für Gäste erreichbar.
- **Helles & dunkles Design** — die App startet im **hellen Design**; per Umschalter (auch für Gäste, am PC oben und mobil im „Mehr"-Menü) jederzeit auf dunkel wechselbar, die Wahl bleibt gespeichert.
- **Laufender News-Ticker** — der „Aktuelles"-Streifen auf der Startseite läuft automatisch durch und pausiert beim Drüberfahren/Antippen.
- **Push-Benachrichtigungen** — via ntfy.sh aufs Handy, sobald ein Transkript fertig ist (mit tippbarem Direktlink).
- **Unterstützen & Feedback** — auf der „Über"-Seite ein **„Buy me a coffee"**-Link (PayPal) und ein **Feedback-Formular** an den Entwickler (mit E-Mail-Fallback).

---

*PodScribe · © 2026 Sven Kompe · Self-Hosted auf Synology · entwickelt mit Claude.*
