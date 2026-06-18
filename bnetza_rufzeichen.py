#!/usr/bin/env python3
# coding: utf-8
"""
Bundesnetzagentur Rufzeichenliste - kombinierter Downloader, Parser & Geocoder
==============================================================================
DF1PAW :-)

Lädt die aktuelle Amateurfunk-Rufzeichenliste der Bundesnetzagentur (PDF),
extrahiert die Rufzeichen in CSV, gibt Statistiken aus, geokodiert
optional die Standorte und kann die Stationen als interaktive Karte
ausgeben.

Basiert auf Skript:
  * bnetza-parser.py    (Joerg Schultze-Lutter, 2021)


Lizenz: GNU GPL v3 oder später.

Abhängigkeiten:
    pip install requests geopy pdfminer.six folium waybackpy

Beispiele:
    # Download und Parse, CSV landet per Default in rufzeichen.csv
    python3 bnetza_rufzeichen.py

    # CSV explizit nach stdout
    python3 bnetza_rufzeichen.py -o -

    # Erste 500 noch nicht gecachte Adressen geokodieren
    python3 bnetza_rufzeichen.py -o rufzeichen.csv --geocode --geo-limit 500 \
        --user-agent "meinprojekt (mail@example.com)"

    # Karte mit Heatmap + POI
    python3 bnetza_rufzeichen.py -o rufzeichen.csv --map karte.html \
        --geocoder nominatim --user-agent "meinprojekt (mail@example.com)"

WICHTIGER HINWEIS zur Geocoder-Nutzung:
    Das schnelle Geokodieren vieler Adressen über Nomination
    verstösst gegen dessen Usage Policy (max. ~1 Anfrage/Sek.)
    Cache + Deduplizierung + --geo-limit reduzieren die Last erheblich; 
    bitte stets eine Kontakt-Mail im User-Agent angeben.

OPTIONAL - amtlicher BKG/AdV-Geocoder (standardmaessig deaktiviert):
    Im Code liegt ein auskommentierter AdvGeocoder (gdz_ortssuche, HK-DE,
    hausnummerngenau). gdz_ortssuche untersagt die persistente Speicherung der
    Ergebnisse - dieses Skript schreibt Koordinaten aber in CSV und Cache. Die
    Aktivierung ist daher bewusst manuell zu treffen (siehe Block bei
    build_geolocator). Open-Source-Projekt: die Entscheidung liegt bei dir.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO, StringIO
from typing import List, Optional, Tuple

import requests

# pdfminer.six
from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser

# geopy
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim


# --------------------------------------------------------------------------- #
# Konstanten
# --------------------------------------------------------------------------- #

_BASE = ("https://data.bundesnetzagentur.de/Bundesnetzagentur/SharedDocs/"
         "Downloads/DE/Sachgebiete/Telekommunikation/Unternehmen_Institutionen/"
         "Frequenzen/Amateurfunk/Rufzeichenliste/")

DEFAULT_PDF_URLS = [
    _BASE + "Rufzeichenliste_AFU1.pdf",   # primäre Datei
    _BASE + "rufzeichenliste_afu.pdf",    # Fallback URL (Altbestand???)
]

# Rufzeichen am Zeilenanfang + Klasse.
# Korrekturen gegenüber dem Original r"^(D[A-D|F-R][0-9][A-Z]{1,3}),\s(A|E),":
#   1. [A-D|F-R] enthielt ein literales '|' (Bug). Korrekt: [A-DF-R].
#   2. Klasse [AEN] statt (A|E): Die Einsteigerklasse "N" (AFuV-Novelle 2024)
#      wurde sonst stillschweigend verworfen.
DEFAULT_CALLSIGN_REGEX = r"^(D[A-DF-R][0-9][A-Z]{1,3}),\s([AEN]),"

# Eine Adresskomponente "PLZ Ort" (5-stellige PLZ als zuverlässiger Anker).
PLZ_REGEX = re.compile(r"^(\d{5})\s+(.+)$")

GERMANY_CENTER = [51.1657, 10.4515]

DEFAULT_OUTPUT_CSV = "rufzeichen.csv"      # CSV wird per Default in diese Datei geschrieben
DEFAULT_ARCHIVE_MAX_AGE_DAYS = 30          # ältere Archivkopien werden erneuert

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bnetza")


# --------------------------------------------------------------------------- #
# Fortschrittsanzeige
# --------------------------------------------------------------------------- #

class Progress:

    def __init__(self, total: Optional[int] = None, *, label: str = "",
                 every: int = 25, stream=sys.stderr):
        self.total = total
        self.label = label
        self.every = every
        self.stream = stream
        self.tty = hasattr(stream, "isatty") and stream.isatty()
        self.n = 0

    def update(self, step: int = 1, extra: str = "") -> None:
        self.n += step
        if self.tty:
            if self.total:
                pct = 100 * self.n / self.total
                self.stream.write(f"\r  {self.label}: {self.n}/{self.total} "
                                  f"({pct:5.1f} %) {extra}        ")
            else:
                self.stream.write(f"\r  {self.label}: {self.n} {extra}        ")
            self.stream.flush()
        elif self.n % self.every == 0 or (self.total and self.n >= self.total):
            if self.total:
                pct = 100 * self.n / self.total
                print(f"  {self.label}: {self.n}/{self.total} ({pct:5.1f} %) {extra}",
                      file=self.stream, flush=True)
            else:
                print(f"  {self.label}: {self.n} {extra}", file=self.stream, flush=True)

    def done(self, extra: str = "") -> None:
        if self.tty:
            self.stream.write("\n")
            self.stream.flush()
        if extra:
            print(f"  {self.label}: fertig - {extra}", file=self.stream, flush=True)


# --------------------------------------------------------------------------- #
# Datenmodell
# --------------------------------------------------------------------------- #

@dataclass
class Record:
    """Ein Rufzeichen-Datensatz mit strukturierten Feldern."""
    callsign: str
    klasse: str
    name: str
    addresses: List[Tuple[str, str, str]] = field(default_factory=list)  # (Strasse, PLZ, Ort)
    coords: Optional[Tuple[float, float]] = None

    @property
    def has_address(self) -> bool:
        return bool(self.addresses)

    @property
    def primary(self) -> Tuple[str, str, str]:
        return self.addresses[0] if self.addresses else ("", "", "")


# --------------------------------------------------------------------------- #
# Download (mehrere Quellen + Internet-Archive-Fallback)
# --------------------------------------------------------------------------- #

def _http_get(url: str, *, timeout: int = 120,
              headers: Optional[dict] = None) -> Optional[requests.Response]:
    try:
        resp = requests.get(url, headers=headers or {"User-Agent": "Mozilla/5.0"},
                            timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        logger.warning("HTTP-Fehler bei %s: %s", url, exc)
        return None


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def wayback_newest_snapshot(url: str, user_agent: str
                            ) -> Optional[Tuple[str, datetime]]:
    """Neuesten erfolgreichen (HTTP 200) Wayback-Snapshot via CDX-Server-API.
    Rückgabe: (archive_url, datetime des Snapshots) oder None.
    """
    try:
        from waybackpy import WaybackMachineCDXServerAPI
        from waybackpy.exceptions import NoCDXRecordFound
    except ImportError:
        logger.warning("waybackpy nicht installiert (pip install waybackpy) - "
                       "Archive-Erkennung übersprungen.")
        return None
    try:
        cdx = WaybackMachineCDXServerAPI(
            url, user_agent=user_agent, filters=["statuscode:200"])
        newest = None
        for snap in cdx.snapshots():
            if newest is None or snap.datetime_timestamp > newest.datetime_timestamp:
                newest = snap
    except NoCDXRecordFound:
        return None
    except Exception as exc:  
        logger.warning("CDX-Abfrage fehlgeschlagen: %s", exc)
        return None
    if newest is None:
        return None
    return newest.archive_url, newest.datetime_timestamp


def wayback_save(url: str, user_agent: str) -> Optional[str]:
    try:
        from waybackpy import WaybackMachineSaveAPI
    except ImportError:
        logger.warning("waybackpy nicht installiert - Archivierung übersprungen.")
        return None
    logger.info("Stoße Archivierung an: %s", url)
    try:
        save_api = WaybackMachineSaveAPI(url, user_agent=user_agent)
        archive_url = save_api.save()
        logger.info("Archiviert: %s", archive_url)
        return archive_url
    except Exception as exc:
        logger.warning("Archivierung fehlgeschlagen: %s", exc)
        return None


def _acquire_from_url(url: str, *, user_agent: str, prefer_archive: bool,
                      archive_if_missing: bool,
                      max_age_days: int) -> Optional[bytes]:
    """Direktdownload einer einzelnen URL Waybackmachine Fallback wenn offline."""
    blob: Optional[bytes] = None

    # 1) Direktdownload
    if not prefer_archive:
        resp = _http_get(url)
        if resp is not None and resp.content:
            blob = resp.content
            logger.info("Direktdownload erfolgreich (%d Bytes).", len(blob))

    # 2) Fallback/bevorzugt: Internet Archive
    if blob is None:
        logger.info("Versuche Internet Archive fuer %s ...", url)
        info = wayback_newest_snapshot(url, user_agent)
        if info is None and archive_if_missing:
            wayback_save(url, user_agent)
            time.sleep(8)
            info = wayback_newest_snapshot(url, user_agent)
        if info:
            archive_url, _ = info
            resp = _http_get(archive_url)
            if resp is not None and resp.content:
                blob = resp.content
                logger.info("Archiv-Download erfolgreich (%d Bytes).", len(blob))
        else:
            logger.info("Keine archivierte Version für diese URL verfügbar.")

    # 3) Direktdownload ok -> Archivkopie prüfen (vorhanden? aktuell genug?)
    elif archive_if_missing:
        info = wayback_newest_snapshot(url, user_agent)
        if info is None:
            logger.info("Noch nicht im Archiv - stoße Archivierung an.")
            wayback_save(url, user_agent)
        else:
            _, ts = info
            age_days = (_utcnow_naive() - ts).days
            if age_days > max_age_days:
                logger.info("Archivkopie ist %d Tage alt (> %d) - erneuere.",
                            age_days, max_age_days)
                wayback_save(url, user_agent)
            else:
                logger.info("Aktuelle Archivkopie vorhanden (%d Tage alt).",
                            age_days)

    return blob


def download_pdf(urls: List[str], *, user_agent: str, prefer_archive: bool = False,
                 archive_if_missing: bool = True,
                 max_age_days: int = DEFAULT_ARCHIVE_MAX_AGE_DAYS) -> Optional[bytes]:
    for i, url in enumerate(urls, 1):
        logger.info("Quelle %d/%d: %s", i, len(urls), url)
        blob = _acquire_from_url(url, user_agent=user_agent,
                                 prefer_archive=prefer_archive,
                                 archive_if_missing=archive_if_missing,
                                 max_age_days=max_age_days)
        if blob:
            return blob
        logger.warning("Quelle %d fehlgeschlagen.", i)
    return None


# --------------------------------------------------------------------------- #
# PDF-Parsing
# --------------------------------------------------------------------------- #

def parse_pdf(file_content: bytes, *,
              callsign_regex: str = DEFAULT_CALLSIGN_REGEX,
              show_progress: bool = True) -> List[str]:
    regex = re.compile(callsign_regex)
    output_string = StringIO()

    parser = PDFParser(BytesIO(file_content))
    document = PDFDocument(parser)
    if not document.is_extractable:
        raise PDFDocument.PDFTextExtractionNotAllowed("PDF erlaubt keine Textextraktion.")

    rsrcmgr = PDFResourceManager()
    device = TextConverter(rsrcmgr, output_string, laparams=LAParams())
    interpreter = PDFPageInterpreter(rsrcmgr, device)

    # Gesamtseitenzahl für die Fortschrittsanzeige ermitteln
    total_pages = None
    try:
        from pdfminer.pdftypes import resolve1
        total_pages = resolve1(document.catalog["Pages"]).get("Count")
    except Exception:
        pass
    progress = Progress(total_pages, label="Seiten") if show_progress else None

    records: List[str] = []
    for page in PDFPage.create_pages(document):
        interpreter.process_page(page)
        lines = output_string.getvalue().split("\n")

        parser_value = ""
        attach_value = False
        for line in lines:
            if regex.search(line):
                if parser_value and regex.search(parser_value):
                    records.append(parser_value)
                parser_value = line
                attach_value = True
            elif "Liste der" in line:
                attach_value = False
                if parser_value and regex.search(parser_value):
                    records.append(parser_value)
                parser_value = ""
            elif "Seite" in line:
                attach_value = True
            elif attach_value and line.strip():
                parser_value += line

        if parser_value and regex.search(parser_value):
            records.append(parser_value)

        output_string.seek(0)
        output_string.truncate(0)

        if progress:
            progress.update(1, extra=f"{len(records)} Rufzeichen")

    if progress:
        progress.done(f"{len(records)} Rufzeichen")
    device.close()
    return records


def parse_record(raw: str, *,
                 callsign_regex: re.Pattern = re.compile(DEFAULT_CALLSIGN_REGEX)
                 ) -> Optional[Record]:
    """Zerlegt einen Roh-Datensatz  per PLZ-Anker.
    Problem: Manchmal Komma, manchmal Semikolon im Datensatz
    """
    raw = raw.strip()
    m = callsign_regex.match(raw)
    if not m:
        return None
    callsign, klasse = m.group(1), m.group(2)

    # Alles nach "CALL, KLASSE," ist Name (+ ggf. Adressen).
    remainder = raw[m.end():].strip()
    # Semikolon zu Komma normalisieren -> einheitlich komma-getrennt.
    remainder = remainder.replace("; ", ", ").replace(";", ", ")
    segments = [s.strip() for s in remainder.split(",") if s.strip()]

    name = segments[0] if segments else ""

    addresses: List[Tuple[str, str, str]] = []
    street_parts: List[str] = []
    for seg in segments[1:]:
        pm = PLZ_REGEX.match(seg)
        if pm:  # PLZ gefunden -> Adresse abschließen
            street = ", ".join(street_parts).strip(", ")
            addresses.append((street, pm.group(1), pm.group(2).strip()))
            street_parts = []
        else:   # noch Strassenbestandteil
            street_parts.append(seg)

    return Record(callsign=callsign, klasse=klasse, name=name, addresses=addresses)


def parse_records(file_content: bytes, *, show_progress: bool = True) -> List[Record]:
    raw_records = parse_pdf(file_content, show_progress=show_progress)
    pattern = re.compile(DEFAULT_CALLSIGN_REGEX)
    out: List[Record] = []
    for raw in raw_records:
        rec = parse_record(raw, callsign_regex=pattern)
        if rec is not None:
            out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# Geocoding
# --------------------------------------------------------------------------- #

def load_cache(path: Optional[str]) -> dict:
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            logger.warning("Cache konnte nicht gelesen werden: %s", path)
    return {}


def save_cache(cache: dict, path: Optional[str]) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)
    except OSError as exc:
        logger.warning("Cache konnte nicht geschrieben werden: %s", exc)


def build_query(rec: Record, suffix: str = "Deutschland") -> Optional[str]:
    """Baut die Geocoding-Abfrage"""
    if not rec.has_address:
        return None
    street, plz, ort = rec.primary
    loc = " ".join(x for x in (plz, ort) if x).strip()
    parts = [x for x in (street, loc) if x]
    if not parts:
        return None
    query = ", ".join(parts)
    return f"{query}, {suffix}" if suffix else query


def build_geolocator(name: str, user_agent: str, timeout: int):
    name = name.lower()
    if name == "nominatim":
        return Nominatim(user_agent=user_agent, timeout=timeout)
    if name == "photon":
        from geopy.geocoders import Photon
        return Photon(user_agent=user_agent, timeout=timeout)
    if name == "arcgis":
        from geopy.geocoders import ArcGIS
        return ArcGIS(timeout=timeout)
    # --- OPTIONAL: amtlicher BKG/AdV-Geocoder, standardmässig deaktiviert ---
    # Zum Aktivieren den AdvGeocoder-Block unten einkommentieren, die naechsten
    # beiden Zeilen einkommentieren und "advgeocode" zu den --geocoder choices
    # hinzufügen. Bitte vorher den Hinweis im AdvGeocoder-Block lesen.
    # if name == "advgeocode":
    #     return AdvGeocoder(min_score=0.95)
    raise ValueError(f"Unbekannter Geocoder: {name}")


# =========================================================================== #
# OPTIONAL (standardmässig DEAKTIVIERT): amtlicher BKG/AdV-Geokodierungsdienst
# --------------------------------------------------------------------------- #
#
# !!! WICHTIGER LIZENZHINWEIS - BITTE BEWUSST ENTSCHEIDEN !!!
# Es gibt den Dienst in zwei Varianten:
#   * gdz_ortssuche    -> frei erreichbar, aber die Ergebnisse dürfen NICHT
#                         persistent gespeichert werden.
#   * gdz_geokodierung -> Ergebnisse DÜRFEN gespeichert werden, erfordert aber
#                         Nutzungsrechte (beim BKG/ZSGT zu erwerben).
# Dieses Skript schreibt geokodierte Koordinaten in die CSV UND in
# geocode-cache.json - also persistent. Wer unten gdz_ortssuche verwendet und
# die Koordinaten so dauerhaft wegschreibt, trifft diese (lizenzrechtliche)
# Entscheidung damit bewusst selbst. Dies ist ein Open-Source-Projekt; die
# Verantwortung liegt bei dir. Nutzungsbedingungen:
#   https://sg.geodatenzentrum.de/web_public/gdz/lizenz/deu/nutzungsbedingungen_hk-de.pdf
#
# Zum Aktivieren: diesen Block einkommentieren, in build_geolocator() den
# advgeocode-Zweig einkommentieren und "advgeocode" zu den --geocoder choices
# (in parse_args) hinzufügen. CSV und Cache werden dann automatisch befüllt,
# da geocode_records jeden build_geolocator-Geocoder gleich behandelt.
#
# class _AdvLocation:
#     def __init__(self, lat, lon, raw):
#         self.latitude, self.longitude, self.raw = lat, lon, raw
#
#
# class AdvGeocoder:
#     """BKG/AdV-Geokodierungsdienst (gdz_ortssuche).
#     min_score verwirft schwache Treffer (Gesamtguete < Schwelle).
#     """
#     def __init__(self, base="https://sg.geodatenzentrum.de/gdz_ortssuche",
#                  min_score: float = 0.95, timeout: int = 10):
#         self.base = base.rstrip("/")
#         self.min_score = min_score
#         self.timeout = timeout
#         self.session = requests.Session()
#
#     def geocode(self, query, exactly_one=True):
#         # ", Deutschland"-Suffix entfernen (Dienst ist DE-only)
#         q = query.replace(", Deutschland", "").strip()
#         try:
#             resp = self.session.get(
#                 self.base + "/geosearch",
#                 params={"query": q, "count": 1, "outputformat": "json"},
#                 timeout=self.timeout,
#             )
#             resp.raise_for_status()
#             features = resp.json().get("features", [])
#         except (requests.RequestException, ValueError):
#             return None
#         if not features:
#             return None
#         feat = features[0]
#         if feat.get("properties", {}).get("score", 0) < self.min_score:
#             return None  # schwacher Treffer -> wie "nicht gefunden"
#         lon, lat = feat["geometry"]["coordinates"]   # GeoJSON: [lon, lat]
#         return _AdvLocation(lat, lon, feat)
# =========================================================================== #


def geocode_records(records: List[Record], *, geocode, cache: dict,
                    cache_path: Optional[str] = None, geo_limit: int = 0,
                    suffix: str = "Deutschland", save_every: int = 25,
                    show_progress: bool = True) -> Tuple[int, int]:
    # geokodierbare Datensätze fuer die Fortschrittsanzeige vorab zählen
    total = sum(1 for r in records if build_query(r, suffix=suffix))
    progress = Progress(total, label="Geocoding") if show_progress else None

    new_requests = 0
    hits = 0
    for rec in records:
        query = build_query(rec, suffix=suffix)
        if not query:
            continue
        if query in cache:
            cached = cache[query]
            rec.coords = tuple(cached) if cached else None
        elif geo_limit == 0 or new_requests < geo_limit:
            location = geocode(query)  
            rec.coords = (location.latitude, location.longitude) if location else None
            cache[query] = list(rec.coords) if rec.coords else None
            new_requests += 1
            if cache_path and new_requests % save_every == 0:
                save_cache(cache, cache_path)
        if rec.coords:
            hits += 1
        if progress:
            progress.update(1, extra=f"{new_requests} neu, {hits} Treffer")

    if progress:
        progress.done(f"{new_requests} neue Anfragen, {hits} Treffer")
    if cache_path:
        save_cache(cache, cache_path)
    return new_requests, hits


# --------------------------------------------------------------------------- #
# Statistik
# --------------------------------------------------------------------------- #

def compute_stats(records: List[Record]) -> dict:
    total = len(records)
    by_class = Counter(r.klasse for r in records)
    with_addr = sum(1 for r in records if r.has_address)
    multi_addr = sum(1 for r in records if len(r.addresses) > 1)
    geocoded = sum(1 for r in records if r.coords)
    top_orte = Counter(r.primary[2] for r in records
                       if r.has_address and r.primary[2]).most_common(10)
    top_plz2 = Counter(r.primary[1][:2] for r in records
                       if r.has_address and r.primary[1]).most_common(10)
    return {
        "total": total,
        "by_class": dict(sorted(by_class.items())),
        "with_addr": with_addr,
        "without_addr": total - with_addr,
        "multi_addr": multi_addr,
        "geocoded": geocoded,
        "top_orte": top_orte,
        "top_plz2": top_plz2,
    }


def print_stats(stats: dict, out=sys.stderr) -> None:
    def line(s=""):
        print(s, file=out)

    total = stats["total"] or 1
    line()
    line("=" * 52)
    line(" Statistik Rufzeichenliste")
    line("=" * 52)
    line(f"  Datensätze gesamt : {stats['total']:>7}")
    line("  Nach Klasse:")
    names = {"A": "Klasse A", "E": "Klasse E", "N": "Klasse N (Einsteiger)"}
    for k, v in stats["by_class"].items():
        line(f"      {names.get(k, k):<22}: {v:>7}  ({100*v/total:4.1f} %)")
    line(f"  mit Standortadresse: {stats['with_addr']:>7}  ({100*stats['with_addr']/total:4.1f} %)")
    line(f"  ohne Adresse       : {stats['without_addr']:>7}  ({100*stats['without_addr']/total:4.1f} %)")
    line(f"  mit Mehrfachadresse: {stats['multi_addr']:>7}")
    if stats["geocoded"]:
        line(f"  geokodiert         : {stats['geocoded']:>7}")
    if stats["top_orte"]:
        line("  Top-Orte:")
        for ort, n in stats["top_orte"]:
            line(f"      {ort:<26}: {n:>6}")
    if stats["top_plz2"]:
        line("  Top PLZ-Regionen (2-stellig):")
        for plz2, n in stats["top_plz2"]:
            line(f"      {plz2}xxx                     : {n:>6}")
    line("=" * 52)


# --------------------------------------------------------------------------- #
# Ausgabe
# --------------------------------------------------------------------------- #

CSV_HEADER = ["rufzeichen", "klasse", "name", "strasse", "plz", "ort", "lat", "lon"]


def write_csv(records: List[Record], out, *, header: bool = True) -> None:
    writer = csv.writer(out, delimiter=";")
    if header:
        writer.writerow(CSV_HEADER)
    for rec in records:
        street, plz, ort = rec.primary
        lat, lon = rec.coords if rec.coords else ("", "")
        writer.writerow([rec.callsign, rec.klasse, rec.name, street, plz, ort, lat, lon])


# Tile-Voreinstellungen. CARTO-Basemaps sind für Einbettung/lokale Nutzung
# gedacht und liefern keine 403, wenn (z.B. bei file://) kein Referer gesendet
# wird. Die rohen OSM-Tiles (tile.openstreetmap.org) verlangen laut Nutzungs-
# richtlinie einen gueltigen Referer und blockieren sonst - daher nicht Default.
TILE_PRESETS = {
    "cartodb-positron": {
        "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        "attr": "&copy; OpenStreetMap-Mitwirkende &copy; CARTO",
        "name": "CARTO Positron",
    },
    "cartodb-voyager": {
        "url": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
        "attr": "&copy; OpenStreetMap-Mitwirkende &copy; CARTO",
        "name": "CARTO Voyager",
    },
    "openstreetmap": {
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attr": "&copy; OpenStreetMap-Mitwirkende",
        "name": "OpenStreetMap",
    },
}


def make_map(records: List[Record], out_html: str, *, mode: str = "both",
             tiles: str = "cartodb-positron") -> bool:
    try:
        import folium
        from folium.plugins import HeatMap, MarkerCluster
    except ImportError:
        logger.error("folium ist nicht installiert (pip install folium).")
        return False

    pts = [(r.coords, r) for r in records if r.coords]
    if not pts:
        logger.error("Keine Geokoordinaten fuer die Karte vorhanden.")
        return False

    points = [list(c) for c, _ in pts]
    center = [sum(p[0] for p in points) / len(points),
              sum(p[1] for p in points) / len(points)]

    preset = TILE_PRESETS.get(tiles, TILE_PRESETS["cartodb-positron"])
    fmap = folium.Map(location=center, zoom_start=6, tiles=None)
    folium.TileLayer(
        tiles=preset["url"], attr=preset["attr"], name=preset["name"],
        referrerPolicy="no-referrer-when-downgrade",
    ).add_to(fmap)
    fmap.get_root().header.add_child(folium.Element(
        '<meta name="referrer" content="no-referrer-when-downgrade">'))

    if mode in ("both", "heatmap"):
        hm = folium.FeatureGroup(name="Heatmap", show=True)
        HeatMap(points, radius=12, blur=15).add_to(hm)
        hm.add_to(fmap)

    if mode in ("both", "markers"):
        cluster = MarkerCluster(name="Rufzeichen (POI)")
        for coords, rec in pts:
            street, plz, ort = rec.primary
            popup = f"<b>{rec.callsign}</b> ({rec.klasse})<br>{rec.name}<br>{street}<br>{plz} {ort}"
            folium.Marker(location=list(coords),
                          popup=folium.Popup(popup, max_width=300),
                          tooltip=rec.callsign).add_to(cluster)
        cluster.add_to(fmap)

    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.save(out_html)
    logger.info("Karte gespeichert: %s (%d Punkte)", out_html, len(points))
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Bundesnetzagentur Rufzeichenliste: Download, Parsing, "
                    "Statistik, Geocoding und Visualisierung.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--url", action="append", dest="urls",
                   help="Download-URL (mehrfach moeglich). Ohne Angabe werden "
                        "die beiden Standard-URLs der BNetzA verwendet.")
    p.add_argument("-o", "--output", default=DEFAULT_OUTPUT_CSV,
                   help="CSV-Ausgabedatei. '-' schreibt nach stdout.")
    p.add_argument("--no-header", action="store_true",
                   help="Keine CSV-Kopfzeile schreiben")
    p.add_argument("--no-stats", action="store_true",
                   help="Keine Statistik ausgeben")
    p.add_argument("--no-progress", action="store_true",
                   help="Keine Fortschrittsanzeige beim Parsen")
    p.add_argument("--geocode", action="store_true",
                   help="Standorte geokodieren (Lat/Lon ergaenzen)")
    # Zum Aktivieren des optionalen amtlichen BKG/AdV-Geocoders: "advgeocode"
    # hier ergaenzen und den AdvGeocoder-Block (s. build_geolocator) einkommentieren.
    p.add_argument("--geocoder", choices=["nominatim", "photon", "arcgis"],
                   default="nominatim", help="Geocoding-Dienst")
    p.add_argument("--geo-limit", type=int, default=0,
                   help="Max. Anzahl *neuer* Geocoding-Anfragen pro Lauf "
                        "(0 = unbegrenzt). Cache-Treffer zaehlen nicht.")
    p.add_argument("--cache", default="geocode-cache.json",
                   help="Pfad zum Geocoding-Cache (JSON)")
    p.add_argument("--user-agent",
                   default="bnetza-rufzeichenliste (bitte Kontakt-Mail eintragen)",
                   help="User-Agent fuer Nominatim/Photon und waybackpy "
                        "(Kontakt-Mail eintragen!)")
    p.add_argument("--min-delay", type=float, default=1.1,
                   help="Mindestabstand zwischen Geocoding-Anfragen (Sekunden)")
    p.add_argument("--map", dest="map_file",
                   help="Karte als HTML speichern (impliziert --geocode)")
    p.add_argument("--map-mode", choices=["heatmap", "markers", "both"],
                   default="both", help="Darstellungsart der Karte")
    p.add_argument("--tiles", choices=list(TILE_PRESETS.keys()),
                   default="cartodb-positron",
                   help="Kartenkacheln. CARTO vermeidet OSM-403 bei lokaler "
                        "(file://) Nutzung.")
    p.add_argument("--prefer-archive", action="store_true",
                   help="Direkt aus dem Internet Archive laden")
    p.add_argument("--no-archive-save", action="store_true",
                   help="Keine Archivierung bei archive.org anstossen")
    p.add_argument("--archive-max-age-days", type=int,
                   default=DEFAULT_ARCHIVE_MAX_AGE_DAYS,
                   help="Archivkopie erneuern, wenn der neueste Snapshot "
                        "aelter als so viele Tage ist.")
    p.add_argument("--pdf-file", help="Lokale PDF statt Download (Debug/Offline)")
    p.add_argument("--limit", type=int, default=0,
                   help="Nur die ersten N Datensaetze verarbeiten (0 = alle)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    do_geocode = args.geocode or bool(args.map_file)
    urls = args.urls if args.urls else DEFAULT_PDF_URLS
    use_stdout = args.output == "-"

    # --- PDF beschaffen ---
    if args.pdf_file:
        with open(args.pdf_file, "rb") as fh:
            file_content = fh.read()
    else:
        file_content = download_pdf(
            urls, user_agent=args.user_agent,
            prefer_archive=args.prefer_archive,
            archive_if_missing=not args.no_archive_save,
            max_age_days=args.archive_max_age_days)
    if not file_content:
        logger.error("Keine PDF-Daten - Abbruch.")
        return 1

    records = parse_records(file_content, show_progress=not args.no_progress)
    if args.limit:
        records = records[: args.limit]
    logger.info("%d Rufzeichen-Datensaetze extrahiert.", len(records))

    if do_geocode:
        geolocator = build_geolocator(args.geocoder, args.user_agent, timeout=10)
        geocode = RateLimiter(geolocator.geocode,
                              min_delay_seconds=args.min_delay,
                              max_retries=2, error_wait_seconds=30.0,
                              swallow_exceptions=True)
        cache = load_cache(args.cache)
        new_req, hits = geocode_records(records, geocode=geocode, cache=cache,
                                        cache_path=args.cache,
                                        geo_limit=args.geo_limit,
                                        show_progress=not args.no_progress)
        logger.info("Geocoding: %d neue Anfragen, %d Treffer gesamt.",
                    new_req, hits)

    if use_stdout:
        write_csv(records, sys.stdout, header=not args.no_header)
    else:
        with open(args.output, "w", newline="", encoding="utf-8") as out_fh:
            write_csv(records, out_fh, header=not args.no_header)
        logger.info("CSV geschrieben: %s (%d Zeilen)", args.output, len(records))

    if not args.no_stats:
        print_stats(compute_stats(records))

    if args.map_file:
        make_map(records, args.map_file, mode=args.map_mode, tiles=args.tiles)

    return 0


if __name__ == "__main__":
    sys.exit(main())
