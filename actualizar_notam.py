
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

# Prefijos reales de los PDF publicados por DINACIA.
PREFIXES = {
    "A": "IPIB",
    "C": "NPIB",
    "D": "DPIB",
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
    """Busca los PDF de las series A, C y D en AIS DINACIA."""

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
                f"{len(matches)} PDF; se esperaba uno. "
                "Revisar publicación AIS DINACIA."
            )

        found[series] = matches[0]

        print(
            f"Serie {series}: {matches[0]}",
            flush=True
        )

    return found


def extract_pdf(url):
    """Descarga un PDF y extrae su texto."""

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
            "Extraccion experimental de PIB DINACIA. "
            "No apto como fuente unica de informacion "
            "operacional. No verifica vigencia, "
            "cancelaciones ni completitud."
        ),
    }

    try:
        urls = discover()

        for series, url in urls.items():

            try:
                pages, text = extract_pdf(url)

                # Identificadores candidatos, no NOTAM validados.
                pattern = (
                    rf"(?<![A-Z0-9])"
                    rf"{series}\s*\d{{4}}\s*/\s*\d{{2}}"
                    rf"(?!\d)"
                )

                ids = sorted({
                    re.sub(r"\s+", "", match.upper())
                    for match in re.findall(
                        pattern,
                        text,
                        re.IGNORECASE
                    )
                })

                result["series"][series] = {
                    "url": url,
                    "pages": pages,
                    "text_chars": len(text),
                    "candidate_count": len(ids),
                    "notam_id_candidates": ids,
                    "raw_text": text,
                }

                print(
                    f"Serie {series}: "
                    f"{pages} paginas, "
                    f"{len(text)} caracteres, "
                    f"{len(ids)} identificadores candidatos. "
                    "PDF OK",
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

    # Guardar el JSON incluso si ocurre un error.
    OUT.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    print(
        json.dumps({
            "status": result["status"],
            "series": list(result["series"]),
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
