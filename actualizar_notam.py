#!/usr/bin/env python3
"""Descarga experimental de PIB DINACIA, series A, C y D.

Genera notam.json con metadatos y texto extraído.
No determina vigencia ni completitud operacional de NOTAM.
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
    "A": "PIBSERIEA",
    "C": "PIBSERIEC",
    "D": "PIBSERIED",
}

session = requests.Session()
session.headers.update({
    "User-Agent": "EA2-CAZA-PIB-monitor/1.0"
})


def get(url):
    response = session.get(url, timeout=40)
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
        matches = []

        for url in links:
            filename = urlparse(url).path.split("/")[-1]

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
                f"{len(matches)} PDF; se esperaba uno"
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
        "errors": [],
        "note": (
            "Extraccion experimental. "
            "No apto como fuente unica de "
            "informacion operacional."
        ),
    }

    try:
        urls = discover()

        for series, url in urls.items():
            try:
                pages, text = extract_pdf(url)

                result["series"][series] = {
                    "url": url,
                    "pages": pages,
                    "text_chars": len(text),
                    "raw_text": text,
                }

                print(
                    f"Serie {series}: "
                    f"{pages} paginas, "
                    f"{len(text)} caracteres",
                    flush=True,
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

    OUT.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps({
            "status": result["status"],
            "series": list(result["series"]),
            "errors": result["errors"],
        }, ensure_ascii=False),
        flush=True,
    )

    print(f"Archivo generado: {OUT.name}", flush=True)

    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
