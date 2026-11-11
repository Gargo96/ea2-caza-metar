
def discover():
    """Localiza los PIB A/C/D mediante sus prefijos oficiales."""
    root = get(BASE + '/ais')
    candidates = links(root.text, root.url)

    prefixes = {
        'A': 'IPIB',
        'C': 'NPIB',
        'D': 'DPIB',
    }

    result = {}

    for series, prefix in prefixes.items():
        pdfs = []

        # Buscar los PDF directamente en la portada AIS.
        for label, url in candidates:
            filename = url.split('?', 1)[0].rsplit('/', 1)[-1]

            if re.match(
                rf'^{prefix}\d+[^/]*\.pdf$',
                filename,
                re.I
            ):
                pdfs.append(url)

        pdfs = list(dict.fromkeys(pdfs))

        if len(pdfs) != 1:
            raise RuntimeError(
                f'Serie {series}: se esperó un PDF '
                f'{prefix} inequívoco; encontrados {len(pdfs)}'
            )

        result[series] = pdfs[0]

    return result
