
#!/usr/bin/env python3
"""Descarga PIB DINACIA y diagnostica la extracción de NOTAM/TWR.

Series A, C y D.
Conserva notam.json y agrega diagnósticos.
NO verifica vigencia, cancelaciones ni disponibilidad operacional.
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

NOTAM_ID = re.compile(
    r"(?<![A-Z0-9])([ACD])\s*(\d{4})\s*/\s*(\d{2})(?!\d)",
    re.IGNORECASE,
)

TWR_PATTERN = re.compile(
    r"\b(?:TWR|TOWER|TORRE|AERODROME\s+CONTROL|"
    r"CONTROL\s+DE\s+AERODROMO)\b",
    re.IGNORECASE,
)

RESTRICTION_PATTERN = re.compile(
    r"\b(?:U/S|UNSERVICEABLE|NOT\s+AVBL|NOT\s+AVAILABLE|"
    r"CLSD|CLOSED|SUSPENDED|OUT\s+OF\s+SERVICE|"
    r"FUERA\s+DE\s+SERVICIO|NO\s+DISPONIBLE)\b",
    re.IGNORECASE,
)

FIELD_E = re.compile(
    r"(?<![A-Z0-9])E\s*\)",
    re.IGNORECASE,
)

session = requests.Session()
session.headers.update({
    "User-Agent": "EA2-CAZA-PIB-monitor/diagnostico-1.0"
})


def get(url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def discover():
    """Encuentra un PDF por cada serie publicada en DINACIA."""

    response = get(AIS_URL)
    soup = BeautifulSoup(response.text, "html.parser")

    links = [
        urljoin(response.url, a["href"])
        for a in soup.select("a[href]")
    ]

    found = {}

    for series, prefix in PREFIXES.items():
        matches = []

        for url in links:
            filename = urlparse(url).path.rsplit("/", 1)[-1]

            if re.fullmatch(
                rf"{prefix}\d+\.pdf",
                filename,
                re.IGNORECASE
            ):
                matches.append(url)

        matches = list(dict.fromkeys(matches))

        if len(matches) != 1:
            raise RuntimeError(
                f"Serie {series}: se encontraron "
                f"{len(matches)} PDF; se esperaba uno."
            )

        found[series] = matches[0]

    return found


def extract_pdf(url):
    """Descarga el PDF y extrae texto por página."""

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

    return len(reader.pages), text


def normalize(text):
    """Normaliza espacios para facilitar la búsqueda."""
    return re.sub(r"\s+", " ", text).strip()


def samples(pattern, text, limit=5, context=100):
    """Devuelve muestras del texto alrededor de coincidencias."""

    results = []

    for match in pattern.finditer(text):
        start = max(0, match.start() - context)
        end = min(len(text), match.end() + context)

        results.append(
            normalize(text[start:end])
        )

        if len(results) >= limit:
            break

    return results


def analyze(text, series):
    """Analiza el texto completo, sin depender de los bloques E)."""

    identifiers = sorted({
        f"{m.group(1).upper()}{m.group(2)}/{m.group(3)}"
        for m in NOTAM_ID.finditer(text)
        if m.group(1).upper() == series
    })

    e_matches = list(FIELD_E.finditer(text))
    twr_matches = list(TWR_PATTERN.finditer(text))
    restriction_matches = list(
        RESTRICTION_PATTERN.finditer(text)
    )

    # Coincidencias cercanas: solamente candidatos,
    # no necesariamente pertenecen al mismo NOTAM.
    proximity_candidates = []

    for match in twr_matches:
        start = max(0, match.start() - 300)
        end = min(len(text), match.end() + 300)
        fragment = text[start:end]

        if RESTRICTION_PATTERN.search(fragment):
            proximity_candidates.append(
                normalize(fragment)
            )

    diagnosis = {
        "identificadores": len(identifiers),
        "campos_e_detectados": len(e_matches),
        "menciones_twr": len(twr_matches),
        "expresiones_indisponibilidad": len(
            restriction_matches
        ),
        "proximidades_twr_indisponibilidad": len(
            proximity_candidates
        ),
        "muestras_twr": samples(
            TWR_PATTERN, text
        ),
        "muestras_campo_e": samples(
            FIELD_E, text, limit=3
        ),
        "muestras_indisponibilidad": samples(
            RESTRICTION_PATTERN, text
        ),
        "muestras_proximidad": proximity_candidates[:5],
    }

    return identifiers, diagnosis


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
        "diagnostics": {},
        "errors": [],
        "note": (
            "Diagnostico experimental de PIB DINACIA. "
            "No valida vigencia ni cancelaciones. "
            "No apto como fuente unica de informacion "
            "operacional."
        ),
    }

    try:
        urls = discover()

        for series, url in urls.items():

            try:
                pages, raw_text = extract_pdf(url)

                identifiers, diagnosis = analyze(
                    raw_text, series
                )

                result["series"][series] = {
                    "url": url,
                    "pages": pages,
                    "text_chars": len(raw_text),
                    "candidate_count": len(identifiers),
                    "notam_id_candidates": identifiers,
                    "raw_text": raw_text,
                }

                result["diagnostics"][series] = diagnosis

                print(
                    f"Serie {series}: "
                    f"{pages} paginas, "
                    f"{len(identifiers)} IDs candidatos. "
                    "PDF OK",
                    flush=True
                )

                print(
                    f"DIAGNOSTICO {series}: "
                    f"campos_E={diagnosis['campos_e_detectados']}, "
                    f"menciones_TWR={diagnosis['menciones_twr']}, "
                    f"indisponibilidad="
                    f"{diagnosis['expresiones_indisponibilidad']}, "
                    f"proximidades="
                    f"{diagnosis['proximidades_twr_indisponibilidad']}",
                    flush=True
                )

                for index, sample in enumerate(
                    diagnosis["muestras_twr"], 1
                ):
                    print(
                        f"MUESTRA TWR {series}-{index}: "
                        f"{sample}",
                        flush=True
                    )

                for index, sample in enumerate(
                    diagnosis["muestras_campo_e"], 1
                ):
                    print(
                        f"MUESTRA E {series}-{index}: "
                        f"{sample}",
                        flush=True
                    )

                for index, sample in enumerate(
                    diagnosis["muestras_proximidad"], 1
                ):
                    print(
                        f"MUESTRA PROXIMIDAD {series}-{index}: "
                        f"{sample}",
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

    # Guardar JSON incluso cuando falla alguna descarga.
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
            "diagnostics": {
                series: {
                    "campos_E": data["campos_e_detectados"],
                    "menciones_TWR": data["menciones_twr"],
                    "indisponibilidad": data[
                        "expresiones_indisponibilidad"
                    ],
                    "proximidades": data[
                        "proximidades_twr_indisponibilidad"
                    ],
                }
                for series, data in result[
                    "diagnostics"
                ].items()
            },
            "errors": result["errors"],
        }, ensure_ascii=False),
        flush=True
    )

    print(
        f"Archivo generado: {OUT.name}",
        flush=True
    )

    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
