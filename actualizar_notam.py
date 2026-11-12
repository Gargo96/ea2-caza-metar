
#!/usr/bin/env python3
"""EA2 CAZA: extracción orientativa de PIB DINACIA (A, C, D).

Los PIB son documentos de referencia, no un feed NOTAM validado.
Este programa NO declara NOTAM vigentes ni TWR disponibles.
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
OUT = Path(__file__).with_name('notam.json')
PREFIXES = {'A': 'IPIB', 'C': 'NPIB', 'D': 'DPIB'}

HEADER = re.compile(
    r'(?<![A-Z0-9])(?P<id>(?P<series>[ACD])\s*\d{4}\s*/\s*\d{2})'
    r'\s+NOTAM(?P<kind>[NRC])?\b', re.I
)
ID = re.compile(r'(?<![A-Z0-9])([ACD])\s*(\d{4})\s*/\s*(\d{2})(?!\d)', re.I)
WINDOW = re.compile(
    r'\bFrom\s+(?P<start>\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2})'
    r'\s*[-–]\s*To\s+(?P<end>\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2})', re.I
)
SCHEDULE = re.compile(r'\bTime\s+Schedule\s*:\s*', re.I)
TWR = re.compile(r'\b(?:TWR|TOWER|TORRE)\b', re.I)
TWR_CLOSED = re.compile(
    r'\b(?:TWR|TOWER|TORRE)\s+(?:CLSD|CLOSED|NOT\s+AVBL|U/S|'
    r'UNSERVICEABLE|FUERA\s+DE\s+SERVICIO|NO\s+DISPONIBLE|CERRADA)\b', re.I
)
TWR_HOURS = re.compile(r'\bTWR\s+(?:HR\s+SER|HOURS?|OPR\s+HR)\b', re.I)
TWR_EQUIPMENT = re.compile(
    r'\b(?:SGL\s+PISTOL|SIGNAL\s+PISTOL|LIGHT\s+GUN|TWR\s+FREQ|TWR\s+EQPT)\b', re.I
)
ICAO = re.compile(r'\bSU[A-Z]{2}\b')

session = requests.Session()
session.headers.update({'User-Agent': 'EA2-CAZA-PIB-monitor/3.0'})


def clean(value):
    return re.sub(r'\s+', ' ', value).strip()


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
        matches = list(dict.fromkeys(
            url for url in links if re.fullmatch(
                rf'{prefix}\d+\.pdf', urlparse(url).path.rsplit('/', 1)[-1], re.I
            )
        ))
        if len(matches) != 1:
            raise RuntimeError(f'Serie {series}: se encontraron {len(matches)} PDF; se esperaba uno')
        found[series] = matches[0]
    return found


def extract_pdf(url):
    content = get(url).content
    if not content.startswith(b'%PDF'):
        raise ValueError('Respuesta no es PDF')
    reader = PdfReader(io.BytesIO(content))
    pages = [page.extract_text() or '' for page in reader.pages]
    raw = '\n'.join(pages)
    if len(raw.strip()) < 100:
        raise ValueError('PDF sin texto extraíble suficiente')
    return len(pages), raw


def parse_utc(value):
    try:
        return datetime.strptime(clean(value), '%d/%m/%y %H:%M').replace(
            tzinfo=timezone.utc
        ).isoformat()
    except (ValueError, TypeError):
        return None


def split_blocks(raw, series):
    matches = [m for m in HEADER.finditer(raw) if m.group('series').upper() == series]
    blocks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        body = clean(raw[match.start():end])
        notam_id = clean(match.group('id')).replace(' ', '').upper()
        blocks.append((notam_id, (match.group('kind') or '').upper(), body))
    return blocks


def classify(text):
    if not TWR.search(text):
        return None
    if TWR_CLOSED.search(text):
        return 'POSIBLE_CIERRE_TWR'
    if TWR_HOURS.search(text):
        return 'CAMBIO_HORARIO_TWR'
    if TWR_EQUIPMENT.search(text):
        return 'EQUIPAMIENTO_TWR'
    return 'REFERENCIA_TWR'


def parse_block(notam_id, kind, body, series, pdf_url):
    icaos = sorted(set(ICAO.findall(body.upper())))
    airport = icaos[0] if len(icaos) == 1 else None
    window = WINDOW.search(body)
    start = parse_utc(window.group('start')) if window else None
    end = parse_utc(window.group('end')) if window else None
    schedule = None
    schedule_match = SCHEDULE.search(body)
    if schedule_match:
        schedule = clean(body[schedule_match.end():])[:350]
    return {
        'id': notam_id,
        'series': series,
        'notam_kind_candidate': kind or 'NO_IDENTIFICADO',
        'aerodrome_candidate': airport,
        'icao_mentions': icaos,
        'from_utc_candidate': start,
        'to_utc_candidate': end,
        'schedule_text_unverified': schedule,
        'twr_classification_candidate': classify(body),
        'requires_review': True,
        'validity_verified': False,
        'cancellation_verified': False,
        'aerodrome_verified': False,
        'source_pdf': pdf_url,
        'raw_text': body[:16000],
    }


def main():
    result = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': AIS_URL,
        'verified': False,
        'status': 'NO_VERIFICADO',
        'twr_status_automation_enabled': False,
        'series': {},
        'notams': [],
        'twr_candidates_unverified': [],
        'errors': [],
        'note': ('Datos orientativos extraídos de PIB. No se verifican cancelaciones, '
                 'completitud, horarios parciales ni vigencia operacional.'),
    }
    try:
        urls = discover()
        for series, url in urls.items():
            try:
                pages, raw = extract_pdf(url)
                blocks = split_blocks(raw, series)
                ids = sorted({f'{m.group(1).upper()}{m.group(2)}/{m.group(3)}'
                              for m in ID.finditer(raw) if m.group(1).upper() == series})
                if ids and not blocks:
                    raise ValueError(f'{len(ids)} identificadores, 0 bloques reconocidos')
                parsed = [parse_block(nid, kind, body, series, url)
                          for nid, kind, body in blocks]
                seen = set()
                duplicates = []
                for item in parsed:
                    if item['id'] in seen:
                        duplicates.append(item['id'])
                    seen.add(item['id'])
                twr_items = [item for item in parsed
                             if item['twr_classification_candidate']]
                result['series'][series] = {
                    'url': url, 'pages': pages, 'text_chars': len(raw),
                    'candidate_count': len(ids), 'blocks_detected': len(blocks),
                    'duplicate_headers': duplicates,
                    'notam_id_candidates': ids, 'raw_text': raw,
                }
                result['notams'].extend(parsed)
                result['twr_candidates_unverified'].extend(twr_items)
                print(f'Serie {series}: {pages} paginas; {len(ids)} IDs; '
                      f'{len(blocks)} bloques; {len(twr_items)} candidatos TWR', flush=True)
                for item in twr_items:
                    print(f"TWR {item['id']}: {item['twr_classification_candidate']} "
                          f"| AD={item['aerodrome_candidate'] or 'SIN_ASIGNAR'} "
                          f"| B={item['from_utc_candidate']} "
                          f"| C={item['to_utc_candidate']} "
                          f"| horario={'SI' if item['schedule_text_unverified'] else 'NO'}",
                          flush=True)
            except Exception as error:
                result['errors'].append(f'Serie {series}: {error}')
        if len(result['series']) == 3 and not result['errors']:
            result['status'] = 'DESCARGA_COMPLETA_PENDIENTE_VALIDACION'
    except Exception as error:
        result['errors'].append(str(error))

    temp = OUT.with_suffix('.json.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')
    temp.replace(OUT)
    print(json.dumps({
        'status': result['status'],
        'series': list(result['series']),
        'notams_extraidos': len(result['notams']),
        'candidatos_twr': len(result['twr_candidates_unverified']),
        'errors': result['errors'],
    }, ensure_ascii=False), flush=True)
    if result['errors']:
        sys.exit(1)


if __name__ == '__main__':
    main()
