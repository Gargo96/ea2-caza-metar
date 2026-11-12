
#!/usr/bin/env python3
"""EA2 CAZA - Descarga y análisis experimental de PIB DINACIA.

Series A, C y D.
Detecta bloques NOTAM y referencias a restricciones TWR.
NO valida vigencia, cancelaciones ni horarios complejos.
NO habilita cambios automáticos en estados TWR.
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
OUT = Path(__file__).resolve().with_name("notam.json")

PREFIXES = {
    "A": "IPIB",
    "C": "NPIB",
    "D": "DPIB",
}

# Identificadores NOTAM: A1703/26, C0123/26, etc.
IDENTIFIER = re.compile(
    r"(?<![A-Z0-9])([ACD])\s*(\d{4})\s*/\s*(\d{2})(?!\d)",
    re.IGNORECASE,
)

# Encabezados observados en los PIB:
# A1703/26 NOTAM From ...
# A1703/26 NOTAMR ...
# A1703/26 NOTAMC ...
# Se admite texto intermedio corto y saltos de línea.
HEADER = re.compile(
    r"(?<![A-Z0-9])"
    r"([ACD])\s*(\d{4})\s*/\s*(\d{2})"
    r"\s+NOTAM(?:N|R|C)?\b",
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

HOURS = re.compile(
    r"\b(?:TWR\s+HR\s+SER|TWR\s+HOURS?|"
    r"TWR\s+OPR\s+HR)\b",
    re.IGNORECASE,
)

EQUIPMENT = re.compile(
    r"\b(?:SGL\s+PISTOL|SIGNAL\s+PISTOL|"
    r"LIGHT\s+GUN|TWR\s+FREQ|TWR\s+EQPT)\b",
    re.IGNORECASE,
)

session = requests.Session()
session.headers.update({
    "User-Agent": "EA2-CAZA-PIB-monitor/2.1"
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
                f"Serie {series}: se encontraron "
                f"{len(matches)} PDF; se esperaba uno."
            )

        found[series] = matches[0]

    return found


def extract_pdf(url):
    response = get(url)

    if not response.content.startswith(b"%PDF"):
        raise ValueError("La respuesta no es un PDF")

    reader = PdfReader(io.BytesIO(response.content))

    text = "\n".join(
        page.extract_text() or ""
        for page in reader.pages
    )

    if len(text.strip()) < 100:
        raise ValueError("PDF sin texto suficiente")

    return len(reader.pages), text


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def find_headers(text, series):
    """Localiza encabezados candidatos de la serie."""

    matches = [
        match
        for match in HEADER.finditer(text)
        if match.group(1).upper() == series
    ]

    # Diagnóstico si la extracción no encuentra encabezados.
    if not matches:
        print(
            f"AVISO Serie {series}: no se encontraron "
            "encabezados con formato ID + NOTAM.",
            flush=True
        )

        # Mostrar ejemplos sin interpretar automáticamente.
        examples = [
            clean(text[max(0, m.start() - 35):m.end() + 90])
            for m in IDENTIFIER.finditer(text)
            if m.group(1).upper() == series
        ][:5]

        for index, example in enumerate(examples, 1):
            print(
                f"EJEMPLO {series}-{index}: {example}",
                flush=True
            )

    return matches


def split_notams(text, series):
    """Divide el PIB por encabezados candidatos."""

    matches = find_headers(text, series)
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
    if not TWR.search(text):
        return None

    # Evitar confundir fallas de equipamiento con
    # indisponibilidad completa de la torre.
    if EQUIPMENT.search(text) and not CLOSURE.search(text):
        return "EQUIPAMIENTO_TWR"

    if CLOSURE.search(text):
        return "POSIBLE_CIERRE_TWR"

    if HOURS.search(text):
        return "MODIFICACION_HORARIO_TWR"

    return "REFERENCIA_TWR"


def analyze(blocks, series, url):
    candidates = []

    for block in blocks:
        classification = classify_twr(block["text"])

        if classification is None:
            continue

        candidates.append({
            "id": block["id"],
            "series": series,
            "classification": classification,
            "aerodrome": None,
            "validity_verified": False,
            "schedule_verified": False,
            "cancellation_verified": False,
            "requires_review": True,
            "source_pdf": url,
            "raw_text": block["text"][:12000],
        })

    return candidates


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
            "Extraccion experimental de PIB DINACIA. "
            "No verifica vigencia, cancelaciones, "
            "horarios parciales ni completitud."
        ),
    }

    try:
        urls = discover()

        for series, url in urls.items():
            try:
                pages, raw_text = extract_pdf(url)

                identifiers = sorted({
                    f"{m.group(1).upper()}{m.group(2)}/{m.group(3)}"
                    for m in IDENTIFIER.finditer(raw_text)
                    if m.group(1).upper() == series
                })

                blocks = split_notams(raw_text, series)

                if identifiers and not blocks:
                    raise ValueError(
                        f"{len(identifiers)} identificadores "
                        "detectados, pero 0 bloques NOTAM. "
                        "Revisar formato del PDF."
                    )

                candidates = analyze(
                    blocks, series, url
                )

                result["series"][series] = {
                    "url": url,
                    "pages": pages,
                    "text_chars": len(raw_text),
                    "candidate_count": len(identifiers),
                    "notam_id_candidates": identifiers,
                    "blocks_detected": len(blocks),
                    "raw_text": raw_text,
                }

                result[
                    "twr_candidates_unverified"
                ].extend(candidates)

                print(
                    f"Serie {series}: "
                    f"{pages} paginas, "
                    f"{len(identifiers)} IDs, "
                    f"{len(blocks)} bloques, "
                    f"{len(candidates)} candidatos TWR.",
                    flush=True
                )

                for candidate in candidates:
                    print(
                        f"TWR {candidate['id']}: "
                        f"{candidate['classification']}",
                        flush=True
                    )

            except Exception as error:
                result["errors"].append(
                    f"Serie {series}: {error}"
                )
                print(
                    f"ERROR Serie {series}: {error}",
                    flush=True
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
