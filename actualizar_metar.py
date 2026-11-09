
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AERODROMOS = [
    "SUDU", "SUMU", "SULS", "SURV",
    "SUSO", "SUPU", "SUMO", "SUCA",
    "SUCM", "SUTB", "SUAG", "SUAA"
]

url = (
    "https://aviationweather.gov/api/data/metar"
    "?ids=" + ",".join(AERODROMOS) + "&format=json"
)

request = urllib.request.Request(
    url,
    headers={"User-Agent": "EA2-CAZA-METAR/1.0"}
)

with urllib.request.urlopen(request, timeout=30) as response:
    metars = json.load(response)

resultado = {
    "actualizado": datetime.now(timezone.utc).isoformat(),
    "fuente": "NOAA Aviation Weather Center",
    "metars": {
        metar["icaoId"]: metar
        for metar in metars
        if metar.get("icaoId") in AERODROMOS
    }
}

Path("metar.json").write_text(
    json.dumps(resultado, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

print("METAR actualizados correctamente")
