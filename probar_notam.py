"""Prueba de descarga y lectura de los PIB A/C/D de AIS Uruguay.
No determina vigencia operacional ni publica NOTAM en EA2 CAZA.
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

AIS = 'https://www.dinacia.gub.uy/ais'
PREFIJOS = {'A': 'IPIB', 'C': 'NPIB', 'D': 'DPIB'}
DESTINO = Path(__file__).resolve().parent / 'notam_prueba.json'


def main():
    sesion = requests.Session()
    sesion.headers.update({'User-Agent': 'EA2-CAZA-PIB-Test/1.0', 'Accept': 'text/html,application/pdf'})
    pagina = sesion.get(AIS, timeout=40)
    pagina.raise_for_status()
    sopa = BeautifulSoup(pagina.text, 'html.parser')
    enlaces = [urljoin(AIS, a['href']) for a in sopa.select('a[href]')]
    resultados = {}
    for serie, prefijo in PREFIJOS.items():
        candidatos = [u for u in enlaces if re.match(r'^' + prefijo + r'\d+[^/]*\.pdf$',
                       urlparse(u).path.rsplit('/', 1)[-1], re.I)]
        if not candidatos:
            raise RuntimeError(f'No se encontró el enlace PDF de la Serie {serie} en AIS')
        url = candidatos[0]
        respuesta = sesion.get(url, timeout=60)
        respuesta.raise_for_status()
        if not respuesta.content.startswith(b'%PDF'):
            raise RuntimeError(f'Serie {serie}: respuesta no es un PDF')
        documento = fitz.open(stream=io.BytesIO(respuesta.content), filetype='pdf')
        texto = '\n'.join(pagina.get_text(sort=True) for pagina in documento)
        if len(texto.strip()) < 100 or 'PIB' not in texto.upper():
            raise RuntimeError(f'Serie {serie}: texto extraído insuficiente')
        resultados[serie] = {
            'url_pdf': url,
            'paginas': len(documento),
            'caracteres_extraidos': len(texto),
            'identificadores_notam_detectados': len(set(re.findall(r'\b' + serie + r'\d{4}/\d{2}\b', texto))),
            'encabezado': ' '.join(texto.split())[:240],
        }
        print(f'Serie {serie}: {len(documento)} páginas; {len(texto)} caracteres; PDF OK')
    salida = {
        'consulta_utc': datetime.now(timezone.utc).isoformat(),
        'fuente': AIS,
        'tipo': 'PRUEBA TECNICA - NO APTO PARA PLANIFICACION OPERACIONAL',
        'series': resultados,
    }
    DESTINO.write_text(json.dumps(salida, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Prueba completa: los tres PDF se descargaron y pudieron leerse.')


if __name__ == '__main__':
    main()
