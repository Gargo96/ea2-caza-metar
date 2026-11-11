
#!/usr/bin/env python3
"""Extracción experimental de PIB DINACIA A/C/D.

Genera notam.json para revisión técnica.
NO verifica vigencia, cancelaciones ni completitud operacional.
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
OUT = Path(__file__).resolve().with_name("notam.json")

PREFIXES = {
    "A": "IPIB",
    "C": "NPIB",
    "D": "DPIB",
}

HEADERS = {
    "User-Agent": "EA2-CAZA-PIB-monitor/0.1",
}

session = requests.Session()
session.headers.update(HEADERS)


def get(url):
    response = session.get(url, timeout=40)
    response.raise_for_status()
    return response


def get_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    return [
        (
            a.get_text(" ", strip=True),
            urljoin(base_url, a["href"]),
        )
        for a in soup.select("a[href]")
    ]


def discover():
    print("=== ENLACES ENCONTRADOS EN AIS DINACIA ===", flush=True)
for etiqueta, url in candidates:
    if any(x in (etiqueta + " " + url).upper() for x in ("PIB", "NOTAM", "SERIE", ".PDF")):
        print(f"ETIQUETA: {etiqueta} | URL: {url}", flush=True)
print("=== FIN DE ENLACES ===", flush=True)
        print("=== ENLACES ENCONTRADOS EN AIS DINACIA ===", flush=True)
    for label, url in candidates:
        print(f"TEXTO: {label} | URL: {url}", flush=True)
    print("=== FIN DE ENLACES ===", flush=True)
    """Busca un PDF inequívoco por serie en la portada AIS."""
    response = get(BASE + "/ais")
    candidates = get_links(response.text, response.url)

    result = {}

    for series, prefix in PREFIXES.items():
        pdfs = []

        for label, url in candidates:
            filename = urlparse(url).path.rsplit("/", 1)[-1]

            if re.match(
                rf"^{prefix}\d+[^/]*\.pdf$",
                filename,
                re.IGNORECASE,
            ):
                pdfs.append(url)

        pdfs = list(dict.fromkeys(pdfs))

        if len(pdfs) != 1:
            raise RuntimeError(
                f"Serie {series}: se esperaba un PDF "
                f"{prefix} inequívoco; encontrados {len(pdfs)}"
            )

        result[series] = pdfs[0]

    return result


def extract_pdf(url):
    response = get(url)

    if not response.content.startswith(b"%PDF"):
        raise ValueError("La respuesta no es un PDF válido")

    reader = PdfReader(io.BytesIO(response.content))

    text = "\n".join(
        page.extract_text() or ""
        for page in reader.pages
    )

    if len(text.strip()) < 100:
        raise ValueError("PDF sin texto extraíble suficiente")

    return len(reader.pages), text


def main():
    result = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "source": BASE + "/ais",
        "verified": False,
        "status": "NO_VERIFICADO",
        "series": {},
        "errors": [],
        "notams": [],
        "note": (
            "EXTRACCION EXPERIMENTAL. "
            "NO APTO PARA PLANIFICACION OPERACIONAL. "
            "No se valida vigencia, cancelaciones, "
            "horarios ni completitud."
        ),
    }

    try:
        urls = discover()

        for series, url in urls.items():
            try:
                pages, text = extract_pdf(url)

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
                        re.IGNORECASE,
                    )
                })

                result["series"][series] = {
                    "url": url,
                    "pages": pages,
                    "text_chars": len(text),
                    "notam_id_candidates": ids,
                    "candidate_count": len(ids),
                }

                print(
                    f"Serie {series}: "
                    f"{pages} paginas; "
                    f"{len(ids)} identificadores candidatos; "
                    "PDF OK",
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

    # Guardar el diagnóstico incluso si hay errores.
    OUT.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "status": result["status"],
                "series": list(result["series"]),
                "errors": result["errors"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    print(
        f"Archivo generado: {OUT.name}",
        flush=True,
    )

    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
