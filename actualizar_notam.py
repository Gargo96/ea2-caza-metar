"""Extrae candidatos NOTAM de los PIB A/C/D de DINACIA.

Salida de revisión técnica, NO validada para uso operacional. No infiere
vigencia, horarios, cancelaciones ni cobertura completa.
"""
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import fitz
import requests
from bs4 import BeautifulSoup

BASE = 'https://www.dinacia.gub.uy/ais'
PREFIJOS = {'A': 'IPIB', 'C': 'NPIB', 'D': 'DPIB'}
DESTINO = Path(__file__).resolve().parent / 'notam.json'
# Reconoce identificadores A1234/26, C0123/26, D0001/26.
IDENT = re.compile(r'(?<![A-Z0-9])([ACD])\s*(\d{4})\s*/\s*(\d{2})(?!\d)', re.I)
# Localización Q) y campo A) cuando están presentes en el texto extraído.
CAMPO_A = re.compile(r'(?m)^\s*A\)\s*([^\n]+)')
CAMPO_Q = re.compile(r'(?m)^\s*Q\)\s*([^\n]+)')


def extraer_bloques(texto, serie):
    """Segmentación preliminar; puede incluir encabezados o texto vecino."""
    coincidencias = [m for m in IDENT.finditer(texto) if m.group(1).upper() == serie]
    salida = []
    vistos = set()
    for indice, m in enumerate(coincidencias):
        codigo = f'{serie}{m.group(2)}/{m.group(3)}'
        if codigo in vistos:
            continue
        vistos.add(codigo)
        fin = coincidencias[indice + 1].start() if indice + 1 < len(coincidencias) else len(texto)
        bloque = texto[m.start():min(fin, m.start() + 12000)].strip()
        a = CAMPO_A.search(bloque)
        q = CAMPO_Q.search(bloque)
        salida.append({
            'id': codigo,
            'serie': serie,
            'texto_extraido': bloque,
            'campo_a_sin_validar': a.group(1).strip() if a else None,
            'campo_q_sin_validar': q.group(1).strip() if q else None,
            'estado': 'SIN_VALIDAR',
        })
    return salida


def main():
    sesion = requests.Session()
    sesion.headers.update({'User-Agent': 'EA2-CAZA-PIB/1.0', 'Accept': 'text/html,application/pdf'})
    respuesta = sesion.get(BASE, timeout=40)
    respuesta.raise_for_status()
    sopa = BeautifulSoup(respuesta.text, 'html.parser')
    enlaces = [urljoin(BASE, a.get('href')) for a in sopa.select('a[href]')]
    series = {}
    avisos = []

    for serie, prefijo in PREFIJOS.items():
        urls = [u for u in enlaces if re.match(
            rf'^{prefijo}\d+[^/]*\.pdf$', urlparse(u).path.rsplit('/', 1)[-1], re.I
        )]
        if len(urls) != 1:
            raise RuntimeError(f'Serie {serie}: se esperó un enlace inequívoco; encontrados {len(urls)}')
        url = urls[0]
        pdf = sesion.get(url, timeout=60)
        pdf.raise_for_status()
        if not pdf.content.startswith(b'%PDF'):
            raise RuntimeError(f'Serie {serie}: la respuesta no es PDF')
        doc = fitz.open(stream=io.BytesIO(pdf.content), filetype='pdf')
        texto = '\n'.join(p.get_text(sort=True) for p in doc)
        if len(texto.strip()) < 100 or 'PIB' not in texto.upper():
            raise RuntimeError(f'Serie {serie}: contenido insuficiente')
        bloques = extraer_bloques(texto, serie)
        if not bloques:
            raise RuntimeError(f'Serie {serie}: no se detectaron identificadores; revisar formato PDF')
        avisos.extend(bloques)
        series[serie] = {'pdf': url, 'paginas': len(doc), 'caracteres': len(texto), 'candidatos': len(bloques)}
        print(f'Serie {serie}: {len(doc)} páginas; {len(bloques)} candidatos; PDF OK')

    salida = {
        'consulta_utc': datetime.now(timezone.utc).isoformat(),
        'fuente': BASE,
        'advertencia': 'EXTRACCION EXPERIMENTAL. NO APTO PARA PLANIFICACION OPERACIONAL. No se valida vigencia, completitud ni horarios.',
        'series': series,
        'notams_sin_validar': avisos,
    }
    temporal = DESTINO.with_suffix('.json.tmp')
    temporal.write_text(json.dumps(salida, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporal.replace(DESTINO)
    print(f'Generado notam.json: {len(avisos)} candidatos sin validar; NO publicar en EA2 CAZA todavía.')


if __name__ == '__main__':
    main()
