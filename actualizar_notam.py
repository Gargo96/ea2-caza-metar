
#!/usr/bin/env python3
"""
EA2 CAZA - PIB DINACIA - Version 5.0

Funciones:
- Descargar PIB de series A, C y D.
- Asociar NOTAM con encabezados de aerodromo.
- Extraer fechas UTC.
- Identificar restricciones TWR.
- Detectar referencias duplicadas entre series.
- Interpretar horarios sencillos.
- Preparar indicadores TWR de referencia.

IMPORTANTE:
No reemplaza la consulta oficial de AIS/AIP/NOTAM.
Los estados son orientativos, no verificacion operacional.
"""

import io
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader


AIS_URL = "https://www.dinacia.gub.uy/ais"
OUTPUT = Path(__file__).with_name("notam.json")

PREFIXES = {
    "A": "IPIB",
    "C": "NPIB",
    "D": "DPIB"
}

HEAD = re.compile(
    r"(?<![A-Z0-9])"
    r"(?P<id>(?P<series>[ACD])\s*\d{4}\s*/\s*\d{2})"
    r"\s+NOTAM(?P<kind>[NRC])?\b",
    re.I
)

SECTION = re.compile(
    r"^\s*"
    r"(?P<name>[A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ0-9 /().,'-]{2,90}?)"
    r"\s*[-–—:]\s*"
    r"(?P<icao>SU[A-Z]{2})"
    r"\s*$",
    re.I
)

ICAO = re.compile(r"\bSU[A-Z]{2}\b", re.I)

WINDOW = re.compile(
    r"\bFrom\s+"
    r"(\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2})"
    r"\s*[-–—]\s*To\s+"
    r"(\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2})",
    re.I
)

TWR = re.compile(r"\b(?:TWR|TOWER|TORRE)\b", re.I)

CLOSED = re.compile(
    r"\b(?:TWR|TOWER|TORRE)\s+"
    r"(?:CLSD|CLOSED|NOT\s+AVBL|U/S|"
    r"UNSERVICEABLE|CERRADA|NO\s+DISPONIBLE)\b",
    re.I
)

HOURS = re.compile(
    r"\bTWR\s+(?:HR\s+SER|HOURS?|OPR\s+HR)\b",
    re.I
)

EQUIPMENT = re.compile(
    r"\b(?:SGL\s+PISTOL|SIGNAL\s+PISTOL|"
    r"LIGHT\s+GUN|TWR\s+FREQ|TWR\s+EQPT)\b",
    re.I
)

SCHEDULE = re.compile(
    r"\bTime\s+Schedule\s*:\s*",
    re.I
)

# Horarios sencillos del tipo BTN 1000/2200.
BTN = re.compile(
    r"\bBTN\s+(\d{4})\s*/\s*(\d{4})\b",
    re.I
)

# Dias de la semana.
WEEKDAYS = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "EA2-CAZA-PIB-reference/5.0"
})


def clean(value):
    return re.sub(r"\s+", " ", value).strip()


def fetch(url):
    response = SESSION.get(url, timeout=60)
    response.raise_for_status()
    return response


def find_pdfs():
    response = fetch(AIS_URL)

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    links = [
        urljoin(response.url, a["href"])
        for a in soup.select("a[href]")
    ]

    result = {}

    for series, prefix in PREFIXES.items():

        matches = list(dict.fromkeys(
            url for url in links
            if re.fullmatch(
                rf"{prefix}\d+\.pdf",
                urlparse(url).path.rsplit("/", 1)[-1],
                re.I
            )
        ))

        if len(matches) != 1:
            raise ValueError(
                f"Serie {series}: encontrados "
                f"{len(matches)} PDF"
            )

        result[series] = matches[0]

    return result


def read_pdf(url):
    data = fetch(url).content

    if not data.startswith(b"%PDF"):
        raise ValueError("Archivo no es PDF")

    reader = PdfReader(io.BytesIO(data))

    pages = [
        page.extract_text(
            extraction_mode="layout"
        ) or ""
        for page in reader.pages
    ]

    if len("".join(pages).strip()) < 100:
        raise ValueError(
            "PDF sin texto suficiente"
        )

    return pages


def parse_date(value):
    try:
        return datetime.strptime(
            clean(value),
            "%d/%m/%y %H:%M"
        ).replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None


def iso(value):
    return value.isoformat() if value else None


def section_from_line(line):
    match = SECTION.fullmatch(clean(line))

    if not match:
        return None

    return {
        "icao": match.group("icao").upper(),
        "name": clean(match.group("name"))
    }


def extract_blocks(pages, series):

    text = "\n".join(pages)

    sections = []
    offset = 0

    for line in text.splitlines(keepends=True):

        section = section_from_line(line)

        if section:
            sections.append((offset, section))

        offset += len(line)

    headers = list(HEAD.finditer(text))

    blocks = []

    for index, match in enumerate(headers):

        if match.group("series").upper() != series:
            continue

        start = match.start()

        next_header = (
            headers[index + 1].start()
            if index + 1 < len(headers)
            else len(text)
        )

        next_section = next(
            (
                pos for pos, _ in sections
                if pos > start
            ),
            len(text)
        )

        end = min(
            next_header,
            next_section
        )

        heading = None

        for pos, section in sections:
            if pos < start:
                heading = section
            else:
                break

        notam_id = re.sub(
            r"\s+",
            "",
            match.group("id")
        ).upper()

        body = clean(text[start:end])

        blocks.append({
            "id": notam_id,
            "kind": (
                match.group("kind") or ""
            ).upper(),
            "body": body,
            "heading": heading
        })

    return blocks, sections


def classify_twr(body):

    if not TWR.search(body):
        return None

    if CLOSED.search(body):
        return "CIERRE_TWR"

    if HOURS.search(body):
        return "HORARIO_TWR"

    if EQUIPMENT.search(body):
        return "EQUIPAMIENTO_TWR"

    return "REFERENCIA_TWR"


def parse_hhmm(value):

    if not re.fullmatch(r"\d{4}", value):
        return None

    hour = int(value[:2])
    minute = int(value[2:])

    if hour > 23 or minute > 59:
        return None

    return hour * 60 + minute


def parse_schedule(text):
    """
    Solo interpreta:
      BTN HHMM/HHMM
      DAILY BTN HHMM/HHMM
      MON TO FRI BTN HHMM/HHMM

    Otras expresiones quedan pendientes.
    """

    if not text:
        return {
            "type": "CONTINUOUS",
            "supported": True
        }

    upper = clean(text).upper()

    # Se exige que el horario completo tenga
    # una estructura sencilla y reconocible.
    pattern = re.fullmatch(
        r"(?:(DAILY|MON TO FRI|MON TO SAT|"
        r"SAT TO SUN)\s+)?"
        r"BTN\s+(\d{4})/(\d{4})",
        upper
    )

    if not pattern:
        return {
            "type": "COMPLEX",
            "supported": False,
            "raw": text
        }

    day_group = pattern.group(1)

    start = parse_hhmm(pattern.group(2))
    end = parse_hhmm(pattern.group(3))

    if start is None or end is None:
        return {
            "type": "INVALID",
            "supported": False,
            "raw": text
        }

    if day_group in (None, "DAILY"):
        days = list(range(7))

    elif day_group == "MON TO FRI":
        days = [0, 1, 2, 3, 4]

    elif day_group == "MON TO SAT":
        days = [0, 1, 2, 3, 4, 5]

    else:
        days = [5, 6]

    return {
        "type": "WEEKLY",
        "supported": True,
        "days": days,
        "start_minute": start,
        "end_minute": end,
        "raw": text
    }


def schedule_active(schedule, now):

    if not schedule["supported"]:
        return None

    if schedule["type"] == "CONTINUOUS":
        return True

    if schedule["type"] != "WEEKLY":
        return None

    days = schedule["days"]

    start = schedule["start_minute"]
    end = schedule["end_minute"]

    current = now.hour * 60 + now.minute

    weekday = now.weekday()

    if start == end:
        return None

    if start < end:
        return (
            weekday in days
            and start <= current < end
        )

    # Intervalo que cruza medianoche.
    previous = (
        now - timedelta(days=1)
    ).weekday()

    return (
        (weekday in days and current >= start)
        or
        (previous in days and current < end)
    )


def parse_notam(block, series, url):

    body = block["body"]

    heading = block["heading"]

    mentions = sorted(set(
        code.upper()
        for code in ICAO.findall(body)
    ))

    heading_icao = (
        heading["icao"] if heading else None
    )

    conflict = bool(
        heading_icao
        and mentions
        and any(
            code != heading_icao
            for code in mentions
        )
    )

    aerodrome = None
    method = "UNASSIGNED"

    if heading_icao and not conflict:
        aerodrome = heading_icao
        method = "SECTION_HEADING"

    elif not heading and len(mentions) == 1:
        aerodrome = mentions[0]
        method = "SINGLE_ICAO_MENTION"

    window = WINDOW.search(body)

    start = (
        parse_date(window.group(1))
        if window else None
    )

    end = (
        parse_date(window.group(2))
        if window else None
    )

    schedule_match = SCHEDULE.search(body)

    schedule_text = None

    if schedule_match:
        schedule_text = clean(
            body[schedule_match.end():]
        )

    # El texto posterior al horario puede
    # contener la descripcion del NOTAM.
    # Por seguridad solo aceptamos horarios
    # sencillos si el campo es exacto.
    schedule = parse_schedule(schedule_text)

    return {
        "id": block["id"],
        "series": series,
        "kind": block["kind"],
        "aerodrome": aerodrome,
        "aerodrome_heading": (
            heading["name"] if heading else None
        ),
        "assignment_method": method,
        "icao_mentions": mentions,
        "icao_conflict": conflict,
        "from_utc": iso(start),
        "to_utc": iso(end),
        "schedule_text": schedule_text,
        "schedule": schedule,
        "twr_classification": classify_twr(body),
        "duplicate_reference": False,
        "source_pdf": url,
        "raw_text": body,
        "verified": False
    }


def mark_duplicate_references(notams):
    """
    No elimina avisos por coincidencia numerica.
    Solo marca referencias sospechosas cuando
    tienen mismo numero, distinto prefijo y
    una de ellas carece de fechas.
    """

    by_number = {}

    for item in notams:
        number = item["id"][1:]

        by_number.setdefault(
            number, []
        ).append(item)

    duplicates = []

    for number, items in by_number.items():

        series = {
            item["series"]
            for item in items
        }

        if len(series) < 2:
            continue

        dated = [
            item for item in items
            if item["from_utc"]
            and item["to_utc"]
        ]

        undated = [
            item for item in items
            if not item["from_utc"]
            and not item["to_utc"]
        ]

        # Coincidir en numero no demuestra
        # que sean el mismo NOTAM.
        # Se marca, sin borrar ninguno.
        if dated and undated:

            for item in undated:
                item["duplicate_reference"] = True

            duplicates.append({
                "number": number,
                "ids": [
                    item["id"]
                    for item in items
                ],
                "status": "POSSIBLE_DUPLICATE"
            })

    return duplicates


def evaluate_twr(notams, now):
    """
    ROJO: cierre TWR detectado dentro de una
    ventana temporal interpretable.

    GRIS: sin datos suficientes o restricciones
    cuya aplicacion no puede determinarse.

    VERDE: reservado para una futura integracion
    con horarios oficiales AIP y NOTAM validados.

    Ningun resultado equivale a una confirmacion
    operacional.
    """

    aerodromes = sorted(set(
        item["aerodrome"]
        for item in notams
        if item["aerodrome"]
    ))

    results = {}

    for icao in aerodromes:

        related = [
            item for item in notams
            if item["aerodrome"] == icao
            and item["twr_classification"]
            and not item["duplicate_reference"]
        ]

        red = []
        uncertain = []

        for item in related:

            if item["twr_classification"] != "CIERRE_TWR":
                continue

            start_text = item["from_utc"]
            end_text = item["to_utc"]

            if not start_text or not end_text:
                uncertain.append(item["id"])
                continue

            start = datetime.fromisoformat(start_text)
            end = datetime.fromisoformat(end_text)

            if not (start <= now <= end):
                continue

            active = schedule_active(
                item["schedule"],
                now
            )

            if active is True:
                red.append(item["id"])

            elif active is None:
                uncertain.append(item["id"])

        if red:
            color = "ROJO"
            reason = "CIERRE_TWR_DETECTADO"

        elif uncertain:
            color = "GRIS"
            reason = "HORARIO_O_VIGENCIA_INDETERMINADA"

        else:
            color = "GRIS"
            reason = "SIN_CONFIRMACION_DE_DISPONIBILIDAD"

        results[icao] = {
            "color": color,
            "reason": reason,
            "notams_closure_candidates": red,
            "notams_uncertain": uncertain,
            "notams_twr": [
                item["id"]
                for item in related
            ],
            "verified": False,
            "reference_only": True
        }

    return results


def main():

    now = datetime.now(timezone.utc)

    result = {
        "generated_at": now.isoformat(),
        "source": AIS_URL,
        "version": "5.0",
        "verified": False,
        "status": "NO_VERIFICADO",
        "twr_status_automation_enabled": False,
        "series": {},
        "notams": [],
        "twr_candidates_unverified": [],
        "duplicate_references": [],
        "twr_reference_status": {},
        "errors": [],
        "note": (
            "Informacion orientativa. "
            "Los PIB no sustituyen los NOTAM "
            "oficiales ni el AIP vigente."
        )
    }

    try:
        urls = find_pdfs()

        for series, url in urls.items():

            try:
                pages = read_pdf(url)

                blocks, sections = extract_blocks(
                    pages, series
                )

                if not blocks:
                    raise ValueError(
                        "No se encontraron NOTAM"
                    )

                parsed = [
                    parse_notam(
                        block, series, url
                    )
                    for block in blocks
                ]

                twr = [
                    item for item in parsed
                    if item["twr_classification"]
                ]

                result["series"][series] = {
                    "url": url,
                    "pages": len(pages),
                    "blocks_detected": len(blocks),
                    "aerodrome_headings": len(sections),
                    "raw_text": "\n".join(pages)
                }

                result["notams"].extend(parsed)

                print(
                    f"Serie {series}: "
                    f"{len(pages)} paginas, "
                    f"{len(blocks)} NOTAM, "
                    f"{len(sections)} encabezados AD, "
                    f"{len(twr)} referencias TWR",
                    flush=True
                )

            except Exception as error:
                result["errors"].append(
                    f"Serie {series}: {error}"
                )

        duplicates = mark_duplicate_references(
            result["notams"]
        )

        result["duplicate_references"] = duplicates

        result["twr_candidates_unverified"] = [
            item
            for item in result["notams"]
            if item["twr_classification"]
            and not item["duplicate_reference"]
        ]

        result["twr_reference_status"] = evaluate_twr(
            result["notams"],
            now
        )

        print(
            f"Referencias duplicadas posibles: "
            f"{len(duplicates)}",
            flush=True
        )

        print(
            "\nESTADO TWR DE REFERENCIA:",
            flush=True
        )

        for icao, state in sorted(
            result["twr_reference_status"].items()
        ):

            print(
                f"{icao}: {state['color']} "
                f"| {state['reason']} "
                f"| cierres={state['notams_closure_candidates']} "
                f"| inciertos={state['notams_uncertain']}",
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

    temp = OUTPUT.with_suffix(".json.tmp")

    temp.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    temp.replace(OUTPUT)

    summary = {
        "status": result["status"],
        "series": list(result["series"]),
        "notams_extraidos": len(result["notams"]),
        "twr_referencias": len(
            result["twr_candidates_unverified"]
        ),
        "duplicados_posibles": len(
            result["duplicate_references"]
        ),
        "aerodromos": len(
            result["twr_reference_status"]
        ),
        "errors": result["errors"]
    }

    print(
        json.dumps(
            summary,
            ensure_ascii=False
        ),
        flush=True
    )

    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
