
#!/usr/bin/env python3
"""PIB DINACIA A/C/D: descarga y candidatos TWR para revisión.

NO valida vigencia, horarios, cancelaciones ni cobertura NOTAM.
Los candidatos NUNCA autorizan cambiar automáticamente estados TWR.
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

BASE = 'https://www.dinacia.gub.uy'
AIS_URL = BASE + '/ais'
OUT = Path(__file__).resolve().with_name('notam.json')
PREFIXES = {'A': 'IPIB', 'C': 'NPIB', 'D': 'DPIB'}
HEAD = re.compile(r'(?<![A-Z0-9])([ACD])\s*(\d{4})\s*/\s*(\d{2})(?!\d)', re.I)
FIELD = re.compile(r'(?m)^\s*([QABCDE])\)\s*(.*)$')
TWR = re.compile(r'\b(?:TWR|TOWER|TORRE|AERODROME CONTROL|AERODROME CTL|CONTROL DE AERODROMO)\b', re.I)
RESTRICTION = re.compile(r'\b(?:U/S|UNSERVICEABLE|NOT AVBL|NOT AVAILABLE|CLSD|CLOSED|SUSPENDED|OUT OF SERVICE|FUERA DE SERVICIO|NO DISPONIBLE|SUSPENDID[OA])\b', re.I)

session = requests.Session()
session.headers.update({'User-Agent': 'EA2-CAZA-PIB-monitor/1.1', 'Accept': 'text/html,application/pdf'})


def get(url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def discover():
    response = get(AIS_URL)
    soup = BeautifulSoup(response.text, 'html.parser')
    links = [urljoin(response.url, a['href']) for a in soup.select('a[href]')]
    found = {}
    for series, prefix in PREFIXES.items():
        matches = list(dict.fromkeys(url for url in links if re.fullmatch(
            rf'{prefix}\d+\.pdf', urlparse(url).path.rsplit('/', 1)[-1], re.I)))
        if len(matches) != 1:
            raise RuntimeError(f'Serie {series}: {len(matches)} PDF encontrados; se esperaba uno. Revisar AIS.')
        found[series] = matches[0]
    return found


def extract_pdf(url):
    response = get(url)
    if not response.content.startswith(b'%PDF'):
        raise ValueError('La respuesta no es un PDF')
    reader = PdfReader(io.BytesIO(response.content))
    text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    if len(text.strip()) < 100:
        raise ValueError('PDF sin texto extraíble suficiente')
    return len(reader.pages), text


def candidate_blocks(text, series):
    """Heurística: los límites de bloque pueden ser incorrectos en PDF."""
    hits = [m for m in HEAD.finditer(text) if m.group(1).upper() == series]
    candidates = []
    seen = set()
    for i, match in enumerate(hits):
        identifier = f'{series}{match.group(2)}/{match.group(3)}'
        if identifier in seen:
            continue
        seen.add(identifier)
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        block = text[match.start():min(end, match.start() + 10000)]
        fields = {}
        field_hits = list(FIELD.finditer(block))
        for j, field_match in enumerate(field_hits):
            key = field_match.group(1)
            field_end = field_hits[j + 1].start() if j + 1 < len(field_hits) else len(block)
            if key not in fields:
                fields[key] = block[field_match.end(1) + 1:field_end].strip()[:5000]
        e_text = fields.get('E', '')
        # Solo texto E) explícito: no buscar TWR en cabeceras vecinas.
        twr_mentioned = bool(TWR.search(e_text))
        restricted = twr_mentioned and bool(RESTRICTION.search(e_text))
        if twr_mentioned:
            candidates.append({
                'id_candidato': identifier,
                'serie': series,
                'campo_a_sin_validar': fields.get('A'),
                'campo_b_sin_validar': fields.get('B'),
                'campo_c_sin_validar': fields.get('C'),
                'campo_d_sin_validar': fields.get('D'),
                'campo_e_sin_validar': e_text,
                'posible_indisponibilidad_twr': restricted,
                'estado': 'REQUIERE_REVISION_HUMANA',
                'texto_bloque_sin_validar': block[:10000],
            })
    return candidates, len(seen)


def main():
    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': AIS_URL,
        'verified': False,
        'status': 'NO_VERIFICADO',
        'series': {},
        'notams': [],
        'twr_candidates_unverified': [],
        'twr_status_automation_enabled': False,
        'errors': [],
        'note': ('Extracción experimental. Los candidatos TWR NO indican vigencia ni '
                 'disponibilidad operacional. No usar para cambiar colores automáticamente.'),
    }
    try:
        urls = discover()
        for series, url in urls.items():
            try:
                pages, raw = extract_pdf(url)
                twr_candidates, count = candidate_blocks(raw, series)
                output['series'][series] = {
                    'url': url, 'pages': pages, 'text_chars': len(raw),
                    'candidate_count': count,
                    'notam_id_candidates': sorted(set(
                        f'{m.group(1).upper()}{m.group(2)}/{m.group(3)}'
                        for m in HEAD.finditer(raw) if m.group(1).upper() == series)),
                    'raw_text': raw,
                }
                for item in twr_candidates:
                    item['source_pdf'] = url
                output['twr_candidates_unverified'].extend(twr_candidates)
                print(f'Serie {series}: {pages} páginas, {count} IDs candidatos, '
                      f'{len(twr_candidates)} menciones TWR en E). PDF OK', flush=True)
            except Exception as error:
                output['errors'].append(f'Serie {series}: {error}')
        if len(output['series']) == 3 and not output['errors']:
            output['status'] = 'DESCARGA_COMPLETA_PENDIENTE_VALIDACION'
    except Exception as error:
        output['errors'].append(str(error))
    temp = OUT.with_suffix('.json.tmp')
    temp.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(OUT)
    print(json.dumps({'status': output['status'],
                      'series': list(output['series']),
                      'candidatos_twr': len(output['twr_candidates_unverified']),
                      'errors': output['errors']}, ensure_ascii=False), flush=True)
    if output['errors']:
        sys.exit(1)


if __name__ == '__main__':
    main()
