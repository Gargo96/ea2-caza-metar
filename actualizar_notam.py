
#!/usr/bin/env python3
"""EA2 CAZA - Extracción experimental de PIB DINACIA.

Descarga series A, C y D, separa bloques NOTAM del formato
PIB y detecta candidatos de restricciones TWR.

No valida integridad, vigencia, cancelaciones ni horarios
complejos. NO habilita estados operacionales automáticos.
"""

import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

BASE = "https://www.dinacia.gub.uy"
AIS_URL = BASE + "/ais"
OUT = Path(__file__).with_name("notam.json")

PREFIXES = {
    "A": "IPIB",
    "C": "NPIB",
    "D": "DPIB",
}

# En los PIB observados, los NOTAM aparecen como:
# A1703/26 NOTAM From ... - To ...
# La expresión exige "NOTAM" para evitar dividir el
# documento por referencias a otros identificadores.
HEADER = re.compile(
    r"(?<![A-Z0-9])([ACD])\s*(\d{4})\s*/\s*(\d{2})"
    r"\s+NOTAM\b",
    re.IGNORECASE,
)

TWR = re.compile(
    r"\b(?:TWR|TOWER|TORRE|AERODROME CONTROL|"
    r"CONTROL DE AERODROMO)\b",
    re.IGNORECASE,
)

CLOSURE = re.compile(
    r"\b(?:TWR\s+CLSD|TWR\s+CLOSED|"
    r"TWR\s+NOT\s+AVBL|TWR\s+U/S|"
    r"TOWER\s+CLOSED|TORRE\s+CERRADA)\b",
    re.IGNORECASE,
)

TWR_HOURS = re.compile(
    r"\b(?:TWR\s+HR\s+SER|TWR\s+HOURS?|"
    r"TWR\s+OPR\s+HR)\b",
    re.IGNORECASE,
)

AFIS = re.compile(r"\bAFIS\b", re.IGNORECASE)

EQUIPMENT = re.compile(
    r"\b(?:SGL\s+PISTOL|SIGNAL\s+PISTOL|"
    r"LIGHT\s+GUN|TWR\s+FREQ|TWR\s+EQPT)\b",
    re.IGNORECASE,
)

DATES = re.compile(
    r"\bFrom\s+(.+?)\s+-\s+To\s+"
    r"(.+?)(?=\.\s|Time\s+Schedule:|$)",
    re.IGNORECASE | re.DOTALL,
)

session = requests.Session()
session.headers.update({
    "User-Agent": "EA2-CAZA-PIB-monitor/2.0"
})


def get(url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def discover():
    response = get(AIS_URL)
    soup = BeautifulSoup(response.text, "html.parser")

    links = [
        urljoin(response.url, a["href"])
        for a in soup.select("a[href]")
    ]

    found = {}

    for series, prefix in PREFIXES.items():
        matches = list(dict.fromkeys(
            url for url in links
            if re.fullmatch(
                rf"{prefix}\d+\.pdf",
                urlparse(url).path.rsplit("/", 1)[-1],
                re.IGNORECASE
            )
        ))

        if len(matches) != 1:
            raise RuntimeError(
                f"Serie {series}: {len(matches)} PDF; "
                "se esperaba uno."
            )

        found[series] = matches[0]

    return found


def extract_pdf(url):
    response = get(url)

    if not response.content.startswith(b"%PDF"):
        raise ValueError("La respuesta no es un PDF")

    reader = PdfReader(io.BytesIO(response.content))

    pages = [
        page.extract_text() or ""
        for page in reader.pages
    ]

    text = "\n".join(pages)

    if len(text.strip()) < 100:
        raise ValueError("PDF sin texto suficiente")

    return len(pages), text


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def split_notams(text, series):
    """Separa candidatos usando encabezados PIB.

    Los límites siguen siendo heurísticos y requieren
    validación contra el PDF original.
    """
    matches = [
        match for match in HEADER.finditer(text)
        if match.group(1).upper() == series
    ]

    blocks = []

    for index, match in enumerate(matches):
        start = match.start()

        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        identifier = (
            f"{series}{match.group(2)}/{match.group(3)}"
        )

        body = clean(text[start:end])

        blocks.append({
            "id": identifier,
            "text": body,
        })

    return blocks


def classify_twr(text):
    """Clasificación orientativa, no operacional."""

    if not TWR.search(text):
        return None

    if CLOSURE.search(text):
        return "POSIBLE_CIERRE_TWR"

    if TWR_HOURS.search(text):
        return "MODIFICACION_HORARIO_TWR"

    if EQUIPMENT.search(text):
        return "EQUIPAMIENTO_TWR"

    return "REFERENCIA_TWR"


def extract_dates(text):
    match = DATES.search(text)

    if not match:
        return {
            "from_text": None,
            "to_text": None,
        }

    return {
        "from_text": clean(match.group(1)),
        "to_text": clean(match.group(2)),
    }


def analyze(blocks, series, source_url):
    candidates = []
    identifiers = set()

    for block in blocks:
        identifier = block["id"]
        body = block["text"]

        identifiers.add(identifier)

        classification = classify_twr(body)

        if not classification:
            continue

        dates = extract_dates(body)

        candidates.append({
            "id": identifier,
            "series": series,
            "classification": classification,
            "from_text": dates["from_text"],
            "to_text": dates["to_text"],
            "aerodrome": None,
            "schedule_verified": False,
            "validity_verified": False,
            "cancellation_verified": False,
            "requires_review": True,
            "source_pdf": source_url,
            "raw_text": body[:12000],
        })

    return sorted(identifiers), candidates


def main():
    result = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "source": AIS_URL,
        "verified": False,
        "status": "NO_VERIFICADO",
        "series": {},
        "notams": [],
        "twr_candidates_unverified": [],
        "twr_status_automation_enabled": False,
        "errors": [],
        "note": (
            "Candidatos extraidos de PIB DINACIA. "
            "No valida vigencia, cancelaciones, horarios "
            "parciales ni completitud. No utilizar como "
            "fuente unica de informacion operacional."
        ),
    }

    try:
        urls = discover()

        for series, url in urls.items():
            try:
                pages, raw_text = extract_pdf(url)

                blocks = split_notams(
                    raw_text, series
                )

                identifiers, candidates = analyze(
                    blocks, series, url
                )

                result["series"][series] = {
                    "url": url,
                    "pages": pages,
                    "text_chars": len(raw_text),
                    "candidate_count": len(identifiers),
                    "notam_id_candidates": identifiers,
                    "raw_text": raw_text,
                }

                result[
                    "twr_candidates_unverified"
                ].extend(candidates)

                print(
                    f"Serie {series}: "
                    f"{pages} paginas, "
                    f"{len(blocks)} bloques, "
                    f"{len(candidates)} candidatos TWR.",
                    flush=True
                )

                for candidate in candidates:
                    print(
                        f"TWR {candidate['id']}: "
                        f"{candidate['classification']} | "
                        f"Desde: {candidate['from_text']} | "
                        f"Hasta: {candidate['to_text']}",
                        flush=True
                    )

            except Exception as error:
                result["errors"].append(
                    f"Serie {series}: {error}"
                )

        if (
            len(result["series"]) == 3
            and not result["errors"]
        ):
            result["status"] = (
                "DESCARGA_COMPLETA_PENDIENTE_VALIDACION"
            )

    except Exception as error:
        result["errors"].append(str(error))

    temp = OUT.with_suffix(".json.tmp")

    temp.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    temp.replace(OUT)

    print(
        json.dumps({
            "status": result["status"],
            "series": list(result["series"]),
            "candidatos_twr": len(
                result["twr_candidates_unverified"]
            ),
            "errors": result["errors"],
        }, ensure_ascii=False),
        flush=True
    )

    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
