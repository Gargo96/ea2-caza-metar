
#!/usr/bin/env python3
"""EA2 CAZA: extracción de referencia de PIB DINACIA.
No constituye verificación operacional ni sustituye AIS/AIP/NOTAM oficiales.
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

AIS_URL = "https://www.dinacia.gub.uy/ais"
OUTPUT = Path(__file__).with_name("notam.json")
PDF_PREFIXES = {"A": "IPIB", "C": "NPIB", "D": "DPIB"}
HEAD = re.compile(r"(?<![A-Z0-9])(?P<id>(?P<series>[ACD])\s*\d{4}\s*/\s*\d{2})\s+NOTAM(?P<kind>[NRC])?\b", re.I)
ICAO = re.compile(r"\bSU[A-Z]{2}\b", re.I)
WINDOW = re.compile(r"\bFrom\s+(\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2})\s*[-–]\s*To\s+(\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2})", re.I)
TWR = re.compile(r"\b(?:TWR|TOWER|TORRE)\b", re.I)
CLOSED = re.compile(r"\b(?:TWR|TOWER|TORRE)\s+(?:CLSD|CLOSED|NOT\s+AVBL|U/S|UNSERVICEABLE|CERRADA|NO\s+DISPONIBLE)\b", re.I)
HOURS = re.compile(r"\bTWR\s+(?:HR\s+SER|HOURS?|OPR\s+HR)\b", re.I)
EQUIP = re.compile(r"\b(?:SGL\s+PISTOL|SIGNAL\s+PISTOL|LIGHT\s+GUN|TWR\s+FREQ|TWR\s+EQPT)\b", re.I)
SCHEDULE = re.compile(r"\bTime\s+Schedule\s*:\s*", re.I)

# Los títulos de sección suelen tener la forma «CARMELO/INTL – SUCM».
# Se exige que el ICAO aparezca al final de una línea para no confundirlo
# con una mención al aeródromo dentro de un NOTAM.
SECTION = re.compile(r"^\s*(?P<name>[A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ0-9 /().,'-]{2,90}?)\s*[-–—:]\s*(?P<icao>SU[A-Z]{2})\s*$", re.I)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "EA2-CAZA-PIB-reference/4.0"})


def normalize(value):
    return re.sub(r"\s+", " ", value).strip()


def fetch(url):
    response = SESSION.get(url, timeout=60)
    response.raise_for_status()
    return response


def find_pdfs():
    response = fetch(AIS_URL)
    soup = BeautifulSoup(response.text, "html.parser")
    urls = [urljoin(response.url, a["href"]) for a in soup.select("a[href]")]
    result = {}
    for series, prefix in PDF_PREFIXES.items():
        matches = list(dict.fromkeys(url for url in urls if re.fullmatch(
            rf"{prefix}\d+\.pdf", urlparse(url).path.rsplit("/", 1)[-1], re.I
        )))
        if len(matches) != 1:
            raise ValueError(f"Serie {series}: se esperó un PDF {prefix}, encontrados {len(matches)}")
        result[series] = matches[0]
    return result


def read_pdf(url):
    data = fetch(url).content
    if not data.startswith(b"%PDF"):
        raise ValueError("El archivo descargado no es un PDF")
    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    if len("".join(pages).strip()) < 100:
        raise ValueError("No se pudo extraer texto suficiente del PDF")
    return pages


def parse_date(value):
    try:
        return datetime.strptime(normalize(value), "%d/%m/%y %H:%M").replace(
            tzinfo=timezone.utc
        ).isoformat()
    except (TypeError, ValueError):
        return None


def section_from_line(line):
    line = normalize(line)
    match = SECTION.fullmatch(line)
    if match:
        return {
            "icao": match.group("icao").upper(),
            "name": normalize(match.group("name"))
        }
    return None


def extract_blocks(pages, series):
    """Lee en orden: encabezado de aeródromo -> NOTAM.

    El encabezado se conserva para cada NOTAM posterior. Los límites se
    calculan usando TODOS los encabezados, no solo los de la serie buscada.
    """
    text = "\n".join(pages)
    lines = text.splitlines(keepends=True)
    sections = []
    offset = 0

    for line in lines:
        section = section_from_line(line)
        if section:
            sections.append((offset, section))
        offset += len(line)

    all_headers = list(HEAD.finditer(text))
    blocks = []

    for index, match in enumerate(all_headers):
        if match.group("series").upper() != series:
            continue

        start = match.start()
        next_header = (
            all_headers[index + 1].start()
            if index + 1 < len(all_headers)
            else len(text)
        )
        next_section = next(
            (pos for pos, _ in sections if pos > start),
            len(text)
        )
        end = min(next_header, next_section)

        prior = [item for pos, item in sections if pos < start]
        heading = prior[-1] if prior else None

        body = normalize(text[start:end])
        notam_id = re.sub(r"\s+", "", match.group("id")).upper()

        blocks.append((
            notam_id,
            (match.group("kind") or "").upper(),
            body,
            heading
        ))

    return blocks, sections


def classify(text):
    if not TWR.search(text):
        return None

    if EQUIP.search(text) and not CLOSED.search(text) and not HOURS.search(text):
        return "EQUIPAMIENTO_TWR"

    if CLOSED.search(text):
        return "POSIBLE_CIERRE_TWR"

    if HOURS.search(text):
        return "CAMBIO_HORARIO_TWR"

    return "REFERENCIA_TWR"


def parse_notam(notam_id, kind, body, heading, series, url):
    mentions = sorted(set(
        code.upper() for code in ICAO.findall(body)
    ))

    heading_icao = heading["icao"] if heading else None

    # Si el encabezado y el texto mencionan ICAO distintos,
    # no se asigna automáticamente.
    conflicting = bool(
        heading_icao
        and mentions
        and any(x != heading_icao for x in mentions)
    )

    aerodrome = heading_icao if not conflicting else None

    if not heading and len(mentions) == 1:
        aerodrome = mentions[0]

    window = WINDOW.search(body)
    schedule = SCHEDULE.search(body)

    return {
        "id": notam_id,
        "series": series,
        "notam_kind_candidate": kind or "NO_IDENTIFICADO",
        "aerodrome_candidate": aerodrome,
        "aerodrome_heading": heading["name"] if heading else None,
        "aerodrome_assignment_method": (
            "SECTION_HEADING"
            if aerodrome and heading
            else "SINGLE_ICAO_MENTION"
            if aerodrome
            else "UNASSIGNED"
        ),
        "icao_mentions": mentions,
        "icao_conflict": conflicting,
        "from_utc_candidate": (
            parse_date(window.group(1)) if window else None
        ),
        "to_utc_candidate": (
            parse_date(window.group(2)) if window else None
        ),
        "schedule_text_unverified": (
            normalize(body[schedule.end():])[:350]
            if schedule else None
        ),
        "twr_classification_candidate": classify(body),
        "requires_review": True,
        "validity_verified": False,
        "cancellation_verified": False,
        "aerodrome_verified": False,
        "source_pdf": url,
        "raw_text": body[:16000],
    }


def main():
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": AIS_URL,
        "verified": False,
        "status": "NO_VERIFICADO",
        "twr_status_automation_enabled": False,
        "series": {},
        "notams": [],
        "twr_candidates_unverified": [],
        "errors": [],
        "note": (
            "PIB orientativo: no confirma vigencia, cancelaciones "
            "ni horarios parciales. Consultar AIS oficial."
        ),
    }

    try:
        urls = find_pdfs()

        for series, url in urls.items():
            try:
                pages = read_pdf(url)
                blocks, sections = extract_blocks(pages, series)

                if not blocks:
                    raise ValueError(
                        "No se encontraron encabezados NOTAM"
                    )

                parsed = [
                    parse_notam(*block, series, url)
                    for block in blocks
                ]

                ids = [item["id"] for item in parsed]

                duplicates = sorted({
                    item for item in ids
                    if ids.count(item) > 1
                })

                twr = [
                    item for item in parsed
                    if item["twr_classification_candidate"]
                ]

                output["series"][series] = {
                    "url": url,
                    "pages": len(pages),
                    "blocks_detected": len(blocks),
                    "duplicate_headers": duplicates,
                    "aerodrome_headings_detected": len(sections),
                    "raw_text": "\n".join(pages),
                }

                output["notams"].extend(parsed)
                output["twr_candidates_unverified"].extend(twr)

                print(
                    f"Serie {series}: {len(pages)} páginas, "
                    f"{len(blocks)} bloques, "
                    f"{len(sections)} encabezados AD, "
                    f"{len(twr)} candidatos TWR",
                    flush=True
                )

                for item in twr:
                    print(
                        f"TWR {item['id']}: "
                        f"{item['twr_classification_candidate']} "
                        f"| AD={item['aerodrome_candidate'] or 'SIN_ASIGNAR'} "
                        f"| método={item['aerodrome_assignment_method']} "
                        f"| conflicto={item['icao_conflict']} "
                        f"| B={item['from_utc_candidate']} "
                        f"| C={item['to_utc_candidate']}",
                        flush=True
                    )

            except Exception as exc:
                output["errors"].append(
                    f"Serie {series}: {exc}"
                )

        if len(output["series"]) == 3 and not output["errors"]:
            output["status"] = (
                "DESCARGA_COMPLETA_PENDIENTE_VALIDACION"
            )

    except Exception as exc:
        output["errors"].append(str(exc))

    temp = OUTPUT.with_suffix(".json.tmp")

    temp.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    temp.replace(OUTPUT)

    print(
        json.dumps({
            "status": output["status"],
            "series": list(output["series"]),
            "notams_extraidos": len(output["notams"]),
            "candidatos_twr": len(
                output["twr_candidates_unverified"]
            ),
            "errors": output["errors"],
        }, ensure_ascii=False),
        flush=True
    )

    if output["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
