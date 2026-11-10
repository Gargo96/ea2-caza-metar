#!/usr/bin/env python3
"""Descarga de diagnóstico de PIB DINACIA. No certifica interpretación operacional."""
import datetime as dt
import io
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

BASE='https://www.dinacia.gub.uy'
OUT=Path(__file__).with_name('notam.json')
HEAD={'User-Agent':'EA2-CAZA-PIB-monitor/0.1 (research; not operational)'}
S=requests.Session();S.headers.update(HEAD)

def get(url):
    r=S.get(url,timeout=35);r.raise_for_status();return r

def links(html,base):
    soup=BeautifulSoup(html,'html.parser')
    return [(a.get_text(' ',strip=True),urljoin(base,a['href'])) for a in soup.select('a[href]')]

def discover():
    # En Drupal la portada AIS enlaza a nodos PIB, que a su vez enlazan al PDF.
    root=get(BASE+'/ais'); candidates=links(root.text,root.url)
    result={}
    for series in 'ACD':
        matches=[]
        for label,url in candidates:
            if re.search(r'Serie\s*'+series+r'\b',label,re.I):matches.append((label,url))
        # La selección depende del HTML publicado por DINACIA; si no es inequívoca, falla cerrada.
        if len(matches)!=1:
            raise RuntimeError(f'Serie {series}: {len(matches)} enlaces inequívocos en portada AIS (se requiere revisión)')
        url=matches[0][1]
        if url.lower().split('?')[0].endswith('.pdf'):
            result[series]=url;continue
        node=get(url)
        pdfs=[u for _,u in links(node.text,node.url) if '.pdf' in u.lower().split('?')[0]]
        pdfs=list(dict.fromkeys(pdfs))
        if len(pdfs)!=1:
            raise RuntimeError(f'Serie {series}: {len(pdfs)} PDF distintos en {url} (revisión necesaria)')
        result[series]=pdfs[0]
    return result

def main():
    now=dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z')
    payload={'generated_at':now,'verified':False,'notams':[], 'source':'AIS DINACIA PIB A/C/D',
             'status':'NO_VERIFICADO','series':{},'errors':[],
             'note':'Solo descarga y extrae texto. NO se utiliza para inferir disponibilidad operacional.'}
    try:
        found=discover()
        for series,url in found.items():
            try:
                r=get(url)
                if not r.content.startswith(b'%PDF'):raise ValueError('La respuesta no es PDF')
                reader=PdfReader(io.BytesIO(r.content))
                content='\n'.join(p.extract_text() or '' for p in reader.pages)
                if len(content.strip())<80:raise ValueError('PDF sin texto extraíble suficiente')
                payload['series'][series]={'url':url,'pages':len(reader.pages),'text_chars':len(content),
                    'notam_id_candidates':sorted(set(re.findall(r'\b[ACD]\d{4}/\d{2}\b',content)))}
            except Exception as e:payload['errors'].append(f'Serie {series}: {e}')
        if len(payload['series'])==3 and not payload['errors']:
            payload['status']='DESCARGA_COMPLETA_PENDIENTE_VALIDACION'
    except Exception as e:payload['errors'].append(str(e))
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':payload['status'],'series':list(payload['series']),'errors':payload['errors']},ensure_ascii=False))
    if payload['errors']:sys.exit(1)

if __name__=='__main__':main()
