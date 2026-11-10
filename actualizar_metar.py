
"""Actualiza metar.json con observaciones NOAA sin perder las anteriores.

No confundir fecha de consulta con hora de observacion. El frontend debe
seguir mostrando VENCIDO cuando reportTime/obsTime sea antiguo.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AERODROMOS = [
    "SUDU", "SUMU", "SULS", "SURV", "SUSO", "SUPU",
    "SUMO", "SUCA", "SUCM", "SUTB", "SUAG", "SUAA",
]

ARCHIVO = Path(__file__).resolve().parent / "metar.json"

URL = (
    "https://aviationweather.gov/api/data/metar?ids="
    + ",".join(AERODROMOS)
    + "&format=json"
)


def fecha_observacion(metar):
    """Devuelve segundos UTC, o None si la fecha no es interpretable."""
    for campo in ("obsTime", "reportTime"):
        valor = metar.get(campo)

        if isinstance(valor, (int, float)) and valor > 0:
            return float(valor)

        if isinstance(valor, str) and valor.strip():
            try:
                return datetime.fromisoformat(
                    valor.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                pass

    return None


def descargar():
    req = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "EA2-CAZA-METAR/2.0 (GitHub Actions)",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
    )

    for intento in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=35) as respuesta:
                datos = json.load(respuesta)

            if not isinstance(datos, list):
                raise ValueError(
                    "NOAA no devolvio una lista JSON de METAR"
                )

            return datos

        except (
            urllib.error.URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            print(
                f"Intento {intento}/3 fallido: {exc}",
                flush=True,
            )

            if intento == 3:
                raise

            time.sleep(5 * intento)


def cargar_anterior():
    if not ARCHIVO.exists():
        return {}

    anterior = json.loads(
        ARCHIVO.read_text(encoding="utf-8")
    )

    if (
        not isinstance(anterior, dict)
        or not isinstance(anterior.get("metars"), dict)
    ):
        raise ValueError(
            "El metar.json anterior tiene una estructura inesperada"
        )

    return {
        k: v
        for k, v in anterior["metars"].items()
        if k in AERODROMOS and isinstance(v, dict)
    }


def main():
    anteriores = cargar_anterior()
    recibidos = descargar()
    validos = {}

    for metar in recibidos:
        if not isinstance(metar, dict):
            continue

        icao = metar.get("icaoId")

        if icao not in AERODROMOS or not metar.get("rawOb"):
            continue

        nueva_fecha = fecha_observacion(metar)

        if nueva_fecha is None:
            print(
                f"Omitido {icao}: sin hora de observacion valida"
            )
            continue

        previo = validos.get(icao)

        if (
            previo is None
            or nueva_fecha > fecha_observacion(previo)
        ):
            validos[icao] = metar

    if not validos:
        raise RuntimeError(
            "NOAA no devolvio METAR validos; "
            "se conserva metar.json anterior"
        )

    nuevos = 0
    sin_cambio = 0

    for icao, metar in validos.items():
        previo = anteriores.get(icao)

        fecha_previa = (
            fecha_observacion(previo)
            if previo
            else None
        )

        fecha_nueva = fecha_observacion(metar)

        if (
            fecha_previa is None
            or fecha_nueva > fecha_previa
        ):
            anteriores[icao] = metar
            nuevos += 1
        else:
            sin_cambio += 1

    ahora = datetime.now(timezone.utc).isoformat()

    resultado = {
        "actualizado": ahora,
        "fuente": "NOAA Aviation Weather Center",
        "metars": {
            icao: anteriores[icao]
            for icao in AERODROMOS
            if icao in anteriores
        },
    }

    temporal = ARCHIVO.with_suffix(".json.tmp")

    temporal.write_text(
        json.dumps(
            resultado,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    temporal.replace(ARCHIVO)

    print(
        f"Consulta UTC: {ahora}; "
        f"NOAA devolvio {len(validos)} aerodromos validos; "
        f"observaciones nuevas: {nuevos}; "
        f"sin cambio: {sin_cambio}; "
        f"guardados: {len(resultado['metars'])}"
    )

    if nuevos == 0:
        print(
            "AVISO: ninguna observacion mas reciente. "
            "Revisar horarios de METAR en NOAA."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
