# bnetza-rufzeichenliste

Python tool to download, parse and geocode the German amateur radio callsign list (Rufzeichenliste) from Bundesnetzagentur. Extracts 70,000+ callsigns from PDF, geocodes addresses via Nominatim/Photon, generates interactive Leaflet heatmaps and exports structured CSV.

---

🇩🇪 [Deutsche Beschreibung weiter unten](#deutsche-beschreibung) · 🇬🇧 [English description below](#english-description)

---

## Deutsche Beschreibung

### Übersicht

Dieses Skript lädt die aktuelle **Amateurfunk-Rufzeichenliste der Bundesnetzagentur** automatisch herunter, extrahiert alle Rufzeichen strukturiert aus dem PDF und kann die Standorte geokodieren sowie als interaktive Karte visualisieren.

Basiert dem ursrpünglichen Script:
- `bnetza-parser.py` von Joerg Schultze-Lutter (2021)

### Funktionen

- **Download** der Rufzeichenliste direkt von der BNetzA (zwei URL-Varianten - warum auch immer???)
- **Internet Archive Fallback** via waybackpy (CDX-Server-API): bei Nichterreichbarkeit der Quelle wird automatisch auf archive.org zurückgegriffen; ist kein Snapshot vorhanden oder älter als 30 Tage, wird eine neue Archivierung angestoßen
- **Robuster PDF-Parser** auf Basis pdfminer.six: erkennt Adressen per PLZ-Anker unabhängig von der internen Formatierung (mit/ohne Semikolon, mit/ohne Adresse, Mehrfachadressen)
- **Strukturierte CSV-Ausgabe** mit den Feldern `rufzeichen`, `klasse`, `name`, `strasse`, `plz`, `ort`, `lat`, `lon`
- **Statistik** nach Klasse (A/E/N), Adressquote, Top-Orte und PLZ-Regionen
- **Geocoding** via Nominatim, Photon oder ArcGIS mit persistentem JSON-Cache, Deduplizierung und iterativem Limit pro Lauf (`--geo-limit`)
- **Interaktive Karte** als HTML (folium): Heatmap und Marker-Cluster mit Rufzeichen-Popups
- **Optionaler amtlicher BKG/AdV-Geocoder** (auskommentiert, bewusst zu aktivieren – siehe Hinweise zur Lizenz)

### Voraussetzungen

- Python 3.8+
- Abhängigkeiten installieren:

```bash
pip install requests geopy pdfminer.six folium waybackpy
```

### Schnellstart

```bash
# PDF laden, parsen, CSV in rufzeichen.csv schreiben, Statistik ausgeben
python bnetza_rufzeichen.py

# CSV explizit nach stdout
python bnetza_rufzeichen.py -o -

# Geocoding: erste 500 neue Adressen geokodieren (iterativ, wiederholbar)
python bnetza_rufzeichen.py --geocode --geo-limit 500 \
    --user-agent "meinprojekt (mail@example.com)"

# Karte mit Heatmap + Marker-Cluster erzeugen
python bnetza_rufzeichen.py --map karte.html \
    --user-agent "meinprojekt (mail@example.com)"

# Lokale PDF verwenden (Offline/Debug)
python bnetza_rufzeichen.py --pdf-file Rufzeichenliste_AFU1.pdf
```

### Alle Optionen

| Option | Standard | Beschreibung |
|---|---|---|
| `--url URL` | *(BNetzA-URLs)* | Download-URL (mehrfach verwendbar) |
| `-o`, `--output` | `rufzeichen.csv` | CSV-Ausgabedatei; `-` für stdout |
| `--no-header` | – | Keine CSV-Kopfzeile |
| `--no-stats` | – | Statistik unterdrücken |
| `--no-progress` | – | Fortschrittsanzeige unterdrücken |
| `--geocode` | – | Geocoding aktivieren |
| `--geocoder` | `nominatim` | Geocoding-Dienst: `nominatim`, `photon`, `arcgis` |
| `--geo-limit N` | `0` (unbegrenzt) | Max. neue Anfragen pro Lauf (Cache-Treffer zählen nicht) |
| `--cache` | `geocode-cache.json` | Pfad zum persistenten Geocoding-Cache |
| `--user-agent` | *(Platzhalter)* | User-Agent für Nominatim/Photon — **Kontakt-Mail eintragen!** |
| `--min-delay` | `1.1` | Mindestabstand zwischen Geocoding-Anfragen (Sekunden) |
| `--map FILE` | – | Karte als HTML speichern (impliziert `--geocode`) |
| `--map-mode` | `both` | `heatmap`, `markers` oder `both` |
| `--tiles` | `cartodb-positron` | Kartenkacheln: `cartodb-positron`, `cartodb-voyager`, `openstreetmap` |
| `--prefer-archive` | – | Direkt aus dem Internet Archive laden |
| `--no-archive-save` | – | Keine Archivierung bei archive.org anstoßen |
| `--archive-max-age-days N` | `30` | Archivkopie erneuern, wenn älter als N Tage |
| `--pdf-file FILE` | – | Lokale PDF statt Download |
| `--limit N` | `0` (alle) | Nur die ersten N Datensätze verarbeiten |

### Iteratives Geocoding

Der Geocoding-Cache (`geocode-cache.json`) wird zwischen Läufen persistent gespeichert. Mit `--geo-limit` lässt sich die Liste in Etappen abarbeiten, ohne den öffentlichen Dienst zu überlasten — jeder Lauf macht genau N neue Anfragen, bereits gecachte Adressen werden übersprungen:

```bash
# Jeden Tag 1000 neue Adressen geokodieren, bis die Liste vollständig ist
python bnetza_rufzeichen.py --geocode --geo-limit 1000 \
    --user-agent "meinprojekt (mail@example.com)"
```

### Optionaler amtlicher BKG/AdV-Geocoder

Im Code liegt ein auskommentierter `AdvGeocoder`, der den amtlichen **BKG/AdV-Geokodierungsdienst** (Datengrundlage: Amtliche Hauskoordinaten HK-DE) anspricht — hausnummerngenau und qualitativ besser als Nominatim für deutsche Adressen.

> **⚠️ Lizenzhinweis:** Der verwendete Endpunkt `gdz_ortssuche` untersagt die **persistente Speicherung** der Ergebnisse. Dieses Skript schreibt Koordinaten in CSV und Cache. Wer den Block aktiviert, trifft diese Entscheidung bewusst selbst. Für persistente Nutzung steht der Dienst `gdz_geokodierung` zur Verfügung (Nutzungsrechte beim BKG/ZSGT erforderlich).

Zum Aktivieren im Quelltext drei Stellen einkommentieren (Anleitung als Kommentar direkt im Code).

### Hinweise zur Geocoder-Nutzungsrichtlinie

Die öffentlichen Dienste Nominatim (OpenStreetMap) und Photon sind **nicht für Massen-Geocoding** ausgelegt (max. ~1 Anfrage/Sekunde). Der kombinierte Einsatz von `--geo-limit` und dem persistenten Cache hält die Last pro Lauf gering.

### CSV-Ausgabeformat

```
rufzeichen;klasse;name;strasse;plz;ort;lat;lon
DA1AA;A;Norman Czora;Leipziger Str. 212;38124;Braunschweig;;
DL9XYZ;A;Max Mustermann;Musterweg 1;80331;München;48.1374;11.5755
DO1NEU;N;Nina Einsteiger;Lindenweg 3;60311;Frankfurt;;
```

`lat`/`lon` sind leer, wenn keine Adresse vorhanden oder das Geocoding noch nicht erfolgt ist.

### Technische Details

- **PDF-Parser:** Zustandsbasierte Zeilenverarbeitung (pdfminer.six); Adressen werden per **PLZ-Anker** (5-stellige PLZ) aus beliebig formatierten Datensätzen extrahiert — unabhängig davon, ob Semikolons oder Kommas als Trenner verwendet werden
- **Regex-Korrekturen** gegenüber dem Original: `[A-D|F-R]` → `[A-DF-R]` (entfernt literales `|`); Klassen `(A|E)` → `[AEN]` (ergänzt Einsteigerklasse N)
- **Archive.org-Integration** via waybackpy CDX-Server-API

### Lizenz

GNU General Public License v3 oder später — wie die ursprünglichen Skripte.

---

## English Description

### Overview

This script automatically downloads the current **German amateur radio callsign list (Rufzeichenliste)** published by the Bundesnetzagentur (Federal Network Agency), parses all callsigns from the PDF into a structured format, optionally geocodes the addresses and can generate an interactive map.

Based on the original script:
- `bnetza-parser.py` by Joerg Schultze-Lutter (2021)

### Features

- **Download** of the callsign list directly from BNetzA
- **Internet Archive fallback** via waybackpy 
- **Robust PDF parser** based on pdfminer.six: extracts addresses using a postal code anchor, regardless of internal formatting (with/without semicolons, with/without address, multiple addresses per entry)
- **Structured CSV output** with fields `rufzeichen`, `klasse`, `name`, `strasse`, `plz`, `ort`, `lat`, `lon`
- **Statistics** by licence class (A/E/N), address coverage, top cities and postal code regions
- **Geocoding** via Nominatim, Photon or ArcGIS with persistent JSON cache, deduplication and per-run request limit (`--geo-limit`)
- **Interactive map** as HTML (folium): heatmap and marker cluster with callsign popups
- **Optional official BKG/AdV geocoder** (commented out, requires deliberate activation — see licence notes)

### Requirements

- Python 3.8+
- Install dependencies:

```bash
pip install requests geopy pdfminer.six folium waybackpy
```

### Quick Start

```bash
# Download, parse, write CSV to rufzeichen.csv, print statistics
python bnetza_rufzeichen.py

# Write CSV to stdout
python bnetza_rufzeichen.py -o -

# Geocode the first 500 new addresses (iterative, repeatable)
python bnetza_rufzeichen.py --geocode --geo-limit 500 \
    --user-agent "myproject (mail@example.com)"

# Generate a map with heatmap + marker cluster
python bnetza_rufzeichen.py --map map.html \
    --user-agent "myproject (mail@example.com)"

# Use a local PDF (offline/debug)
python bnetza_rufzeichen.py --pdf-file Rufzeichenliste_AFU1.pdf
```

### All Options

| Option | Default | Description |
|---|---|---|
| `--url URL` | *(BNetzA URLs)* | Download URL (repeatable) |
| `-o`, `--output` | `rufzeichen.csv` | CSV output file; `-` for stdout |
| `--no-header` | – | Omit CSV header row |
| `--no-stats` | – | Suppress statistics output |
| `--no-progress` | – | Suppress progress display |
| `--geocode` | – | Enable geocoding |
| `--geocoder` | `nominatim` | Geocoding backend: `nominatim`, `photon`, `arcgis` |
| `--geo-limit N` | `0` (unlimited) | Max. new requests per run (cache hits don't count) |
| `--cache` | `geocode-cache.json` | Path to the persistent geocoding cache |
| `--user-agent` | *(placeholder)* | User-Agent for Nominatim/Photon — **add your contact email!** |
| `--min-delay` | `1.1` | Minimum delay between geocoding requests (seconds) |
| `--map FILE` | – | Save map as HTML (implies `--geocode`) |
| `--map-mode` | `both` | `heatmap`, `markers` or `both` |
| `--tiles` | `cartodb-positron` | Map tiles: `cartodb-positron`, `cartodb-voyager`, `openstreetmap` |
| `--prefer-archive` | – | Load directly from Internet Archive |
| `--no-archive-save` | – | Do not trigger archival at archive.org |
| `--archive-max-age-days N` | `30` | Renew archive copy if older than N days |
| `--pdf-file FILE` | – | Use local PDF instead of downloading |
| `--limit N` | `0` (all) | Process only the first N records |

### Iterative Geocoding

The geocoding cache (`geocode-cache.json`) persists between runs. Combined with `--geo-limit`, the full list can be processed in batches without overloading public services — each run makes exactly N new requests, already-cached addresses are skipped:

```bash
# Geocode 1,000 new addresses per day until the list is complete
python bnetza_rufzeichen.py --geocode --geo-limit 1000 \
    --user-agent "myproject (mail@example.com)"
```

### Optional Official BKG/AdV Geocoder

The source code contains a commented-out, dormant `AdvGeocoder` that queries the official **BKG/AdV geocoding service** (official building coordinates HK-DE) — address-level accurate is superior to Nominatim for German addresses.

> **⚠️ Licence notice:** The endpoint `gdz_ortssuche` prohibits **persistent storage** of results. This script writes coordinates to CSV and cache. Anyone who activates this block makes that decision deliberately. For persistent use, the `gdz_geokodierung` service is available (usage rights must be obtained from BKG/ZSGT).

Activate by uncommenting three locations in the source code (instructions are provided as inline comments).

### Geocoder Usage Policy

The public Nominatim (OpenStreetMap) and Photon services are **not intended for quick bulk geocoding** (max. ~1 request/second). Using `--geo-limit` together with the persistent cache keeps the per-run load low.

### CSV Output Format

```
rufzeichen;klasse;name;strasse;plz;ort;lat;lon
DA1AA;A;Norman Czora;Leipziger Str. 212;38124;Braunschweig;;
DL9XYZ;A;Max Mustermann;Musterweg 1;80331;München;48.1374;11.5755
DO1NEU;N;Nina Einsteiger;Lindenweg 3;60311;Frankfurt;;
```

`lat`/`lon` are empty if no address is available or geocoding has not yet been run.

### Technical Details

- **PDF parser:** State-based line processing (pdfminer.six); addresses are extracted using a **postal code anchor** (5-digit PLZ) regardless of formatting — works with semicolons, commas or no separator at all
- **Regex fixes** compared to the originals: `[A-D|F-R]` → `[A-DF-R]` (removes literal `|`); licence classes `(A|E)` → `[AEN]` (adds novice class N introduced in 2024)
- **archive.org integration** via waybackpy CDX Server API

### Credits

- Original parser: Joerg Schultze-Lutter (2021)


### Licence

GNU General Public License v3 or later — consistent with the original scripts.
