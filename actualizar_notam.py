
"""Extracción experimental de candidatos NOTAM de los PIB A/C/D de DINACIA.

NO APTO PARA USO OPERACIONAL. La extracción no certifica completitud,
vigencia, cancelaciones, horarios ni correspondencia con el PIB original.
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

# Inicio de aviso: ID al comienzo de línea, opcionalmente seguido de NOTAMN/R/C.
# No se corta por referencias al mismo ID en medio de un párrafo.
INICIO = re.compile(
    r'(?m)^[ \t]*([ACD])[ \t]*(\d{4})[ \t]*/[ \t]*(\d{2})'
    r'(?=[ \t]|$|\bNOTAM[NR C]\b)', re.I
)
TIPO = re.compile(r'\bNOTAM([NRC])\b', re.I)
CAMPO_A = re.compile(r'(?m)^\s*A\)\s*([^\n]+)', re.I)
CAMPO_Q = re.compile(r'(?m)^\s*Q\)\s*([^\n]+)', re.I)
CAMPO_B = re.compile(r'(?m)^\s*B\)\s*([^\n]+)', re.I)
CAMPO_C = re.compile(r'(?m)^\s*C\)\s*([^\n]+)', re.I)
CAMPO_D = re.compile(r'(?m)^\s*D\)\s*([^\n]+)', re.I)
CAMPO_E = re.compile(r'(?m)^\s*E\)\s*([^\n]+)', re.I)
OTRO_ID = re.compile(r'\b([ACD]\d{4}/\d{2})\b', re.I)


def campo(patron, texto):
    m = patron.search(texto)
    return m.group(1).strip() if m else None


def extraer_bloques(texto, serie):
    """Devuelve candidatos, nunca NOTAM certificados ni vigentes."""
    coincidencias = [
        m for m in INICIO.finditer(texto)
        if m.group(1).upper() == serie
    ]
    salida = []
    vistos = set()

    for indice, m in enumerate(coincidencias):
        codigo = f'{serie}{m.group(2)}/{m.group(3)}'
        fin = (
            coincidencias[indice + 1].start()
            if indice + 1 < len(coincidencias)
            else len(texto)
        )
        bloque = texto[m.start():fin].strip()
        alertas = []

        if codigo in vistos:
            alertas.append('IDENTIFICADOR_REPETIDO_EN_PIB')
        vistos.add(codigo)

        if len(bloque) < 45:
            alertas.append('BLOQUE_MUY_CORTO')
        if len(bloque) > 12000:
            alertas.append('BLOQUE_MUY_LARGO')

        tipo = TIPO.search(bloque[:250])
        if not tipo:
            alertas.append('TIPO_NOTAM_NO_IDENTIFICADO')

        a = campo(CAMPO_A, bloque)
        q = campo(CAMPO_Q, bloque)
        b = campo(CAMPO_B, bloque)
        c = campo(CAMPO_C, bloque)
        d = campo(CAMPO_D, bloque)
        e = campo(CAMPO_E, bloque)

        if not a:
            alertas.append('CAMPO_A_NO_IDENTIFICADO')
        if not b or not c:
            alertas.append('PERIODO_NO_IDENTIFICADO')
        if not e:
            alertas.append('CAMPO_E_NO_IDENTIFICADO')
        if not q:
            alertas.append('CAMPO_Q_NO_IDENTIFICADO')

        referencias = sorted(set(
            x.upper()
            for x in OTRO_ID.findall(bloque)
            if x.upper() != codigo
        ))

        salida.append({
            'id': codigo,
            'serie': serie,
            'tipo_sin_validar': (
                f'NOTAM{tipo.group(1).upper()}'
                if tipo else None
            ),
            'texto_extraido': bloque,
            'campo_a_sin_validar': a,
            'campo_q_sin_validar': q,
            'campo_b_sin_validar': b,
            'campo_c_sin_validar': c,
            'campo_d_sin_validar': d,
            'campo_e_sin_validar': e,
            'referencias_sin_validar': referencias,
            'alertas_revision': alertas,
            'estado': 'SIN_VALIDAR',
        })

    return salida


def main():
    sesion = requests.Session()
    sesion.headers.update({
        'User-Agent': 'EA2-CAZA-PIB/1.0',
        'Accept': 'text/html,application/pdf'
    })

    respuesta = sesion.get(BASE, timeout=40)
    respuesta.raise_for_status()

    sopa = BeautifulSoup(respuesta.text, 'html.parser')
    enlaces = [
        urljoin(BASE, a.get('href'))
        for a in sopa.select('a[href]')
    ]

    series = {}
    avisos = []

    for serie, prefijo in PREFIJOS.items():
        urls = sorted(set(
            u for u in enlaces
            if re.match(
                rf'^{prefijo}\d+[^/]*\.pdf$',
                urlparse(u).path.rsplit('/', 1)[-1],
                re.I
            )
        ))

        if len(urls) != 1:
            raise RuntimeError(
                f'Serie {serie}: se esperaba un PDF inequívoco; '
                f'encontrados {len(urls)}'
            )

        url = urls[0]
        pdf = sesion.get(url, timeout=60)
        pdf.raise_for_status()

        if not pdf.content.startswith(b'%PDF'):
            raise RuntimeError(
                f'Serie {serie}: respuesta no es PDF'
            )

        with fitz.open(
            stream=io.BytesIO(pdf.content),
            filetype='pdf'
        ) as doc:
            paginas = len(doc)
            texto = '\n'.join(
                p.get_text(sort=True) for p in doc
            )

        if len(texto.strip()) < 100 or 'PIB' not in texto.upper():
            raise RuntimeError(
                f'Serie {serie}: contenido insuficiente'
            )

        bloques = extraer_bloques(texto, serie)

        if not bloques:
            raise RuntimeError(
                f'Serie {serie}: no se detectaron candidatos; '
                'revisar el PDF'
            )

        avisos.extend(bloques)

        con_alertas = sum(
            bool(x['alertas_revision'])
            for x in bloques
        )

        series[serie] = {
            'pdf': url,
            'paginas': paginas,
            'caracteres': len(texto),
            'candidatos': len(bloques),
            'candidatos_con_alertas': con_alertas,
        }

        print(
            f'Serie {serie}: {paginas} páginas; '
            f'{len(bloques)} candidatos; '
            f'{con_alertas} con alertas; PDF OK'
        )

    salida = {
        'consulta_utc': datetime.now(timezone.utc).isoformat(),
        'fuente': BASE,
        'advertencia': (
            'EXTRACCION EXPERIMENTAL. NO APTO PARA PLANIFICACION '
            'OPERACIONAL. No se valida vigencia, completitud, '
            'cancelaciones, horarios ni exactitud del texto.'
        ),
        'series': series,
        'notams_sin_validar': avisos,
    }

    temporal = DESTINO.with_suffix('.json.tmp')

    temporal.write_text(
        json.dumps(
            salida,
            ensure_ascii=False,
            indent=2
        ) + '\n',
        encoding='utf-8'
    )

    temporal.replace(DESTINO)

    print(
        f'Generado notam.json: {len(avisos)} candidatos '
        'SIN VALIDAR. NO publicar en EA2 CAZA todavía.'
    )


if __name__ == '__main__':
    main()
