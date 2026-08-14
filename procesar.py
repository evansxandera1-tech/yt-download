"""
yt-download — Descargar subtítulos, parafrasear con Gemini y subir a Drive
=============================================================================
Versión: 2.1

Pensado para correr dentro de GitHub Actions (workflow_dispatch), sin
depender del celular/Termux. Flujo:

  1. Recibe uno o varios canales/links de YouTube separados por coma (por
     variable de entorno, pasada desde el input del workflow).
  2. Lista los videos recientes de cada canal (o usa el video puntual si se
     pasó un link directo) y baja el subtítulo automático en español de
     cada uno con yt-dlp (sin descargar audio ni video).
  3. Si un canal ya no tiene contenido nuevo (todos sus videos recientes
     ya están en el historial), pasa automáticamente al siguiente canal
     de la lista hasta completar la cantidad de videos pedida (max_videos)
     o hasta agotar todos los canales.
  4. Sube el texto TAL CUAL (crudo, sin parafrasear) a una carpeta de
     Google Drive. El parafraseo con IA queda como un paso aparte,
     opcional, para hacer después ya con el texto seguro en Drive.
     Si se quiere volver a parafrasear en este mismo script, activar
     la variable de entorno PARAFRASEAR=1.
  5. Guarda el ID de cada video procesado en descargados.txt para no
     repetirlo en corridas futuras.

Requiere estas variables de entorno (Secrets del repo en GitHub Actions):
    GDRIVE_CLIENT_ID
    GDRIVE_CLIENT_SECRET
    GDRIVE_REFRESH_TOKEN
    GEMINI_API_KEY
"""

import io
import os
import re
import sys
import time

import requests
import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

CARPETA_SALIDA = "transcripciones"
os.makedirs(CARPETA_SALIDA, exist_ok=True)

DRIVE_FOLDER_NAME = "yt-download"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELO = os.environ.get("GEMINI_MODELO", "gemini-3.6-flash").strip()
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELO}:generateContent"
)
GEMINI_TIMEOUT_SEG = 240

GEMINI_PROMPT_PARAFRASEAR = """Sos un editor de guiones en español. Te paso el subtítulo automático de un video de YouTube, que puede tener palabras mal reconocidas o mal puntuadas.

Tu tarea, en un solo paso:
1. Corregí los errores de reconocimiento (palabras que claramente están mal, cortadas o repetidas por ser subtítulo automático).
2. Reescribí el texto cambiando palabras (sinónimos) y la estructura de las oraciones, manteniendo EXACTAMENTE la misma historia, los mismos hechos y el mismo sentido. No inventes ni agregues información nueva.
3. NO resumas ni acortes el texto: el guion final debe cubrir todos los mismos hechos, momentos y detalles del original, con una extensión similar (no una versión condensada).
4. Puntuá el texto pensando en que lo va a leer una voz sintética: usá comas para pausas cortas, puntos entre ideas, evitá oraciones de más de 20 palabras, y evitá paréntesis, guiones largos o comillas anidadas que puedan confundir a un lector de voz.
5. Releélo una vez antes de responder y corregí frases repetidas o puntuación que no ayude a una lectura natural en voz alta.
6. Devolvé ÚNICAMENTE el guion final en texto corrido. No agregues notas, comentarios, encabezados, aclaraciones sobre el formato, ni ningún texto que no sea parte de la historia misma. Tu respuesta debe terminar en el último punto de la historia, sin nada después.

Subtítulo original:
{texto}"""


_PATRONES_ARTEFACTO = [
    r"\n\s*\*+\s*\*?.*$",
    r"\n\s*(Nota|Draft(ing)?|Aclaraci[oó]n)[:\-].*$",
]


def _limpiar_artefactos(texto):
    limpio = texto
    for patron in _PATRONES_ARTEFACTO:
        limpio = re.sub(patron, "", limpio, flags=re.IGNORECASE | re.DOTALL)
    return limpio.strip()


def log(msg):
    print(f"[yt-download] {msg}", flush=True)


def _llamar_gemini(prompt_template, texto):
    if not GEMINI_API_KEY:
        return texto
    body = {
        "contents": [{"parts": [{"text": prompt_template.format(texto=texto)}]}],
        "generationConfig": {"maxOutputTokens": 32768},
    }

    ultimo_error = None
    for intento in range(1, 6):
        try:
            resp = requests.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json=body,
                timeout=GEMINI_TIMEOUT_SEG,
            )
            if resp.status_code == 429:
                espera = 20 * intento
                log(f"  ⏳ Gemini: límite de peticiones (429). Esperando {espera}s antes de reintentar ({intento}/5)...")
                time.sleep(espera)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.exceptions.RequestException as e:
            ultimo_error = e
            log(f"  ⚠️  Error llamando a Gemini (intento {intento}/5): {e}")
            time.sleep(10 * intento)
    else:
        log(f"  ❌ Gemini no respondió tras varios intentos ({ultimo_error}). Se usa el texto sin cambios para este tramo.")
        return texto

    candidatos = data.get("candidates") or []
    if not candidatos:
        motivo = data.get("promptFeedback", {}).get("blockReason", "desconocido")
        log(f"  ⚠️  Gemini no devolvió candidatos (motivo: {motivo}). Se usa el texto sin cambios para este tramo.")
        return texto

    candidato = candidatos[0]
    finish_reason = candidato.get("finishReason", "")
    if finish_reason == "MAX_TOKENS":
        log("  ⚠️  Gemini cortó la respuesta por límite de tokens de salida (MAX_TOKENS). El bloque puede haber quedado incompleto.")

    partes = candidato.get("content", {}).get("parts")
    if not partes:
        motivo = finish_reason or "desconocido"
        log(f"  ⚠️  Gemini devolvió una respuesta sin contenido (finishReason: {motivo}). Se usa el texto sin cambios para este tramo.")
        return texto

    return partes[0].get("text", texto).strip()


PALABRAS_POR_PARTE = 2200
PAUSA_ENTRE_LLAMADAS_SEG = 8


def _partir_en_bloques(texto, palabras_por_parte=PALABRAS_POR_PARTE):
    oraciones = re.split(r"(?<=[.!?])\s+", texto)
    bloques = []
    actual = []
    palabras_actual = 0
    for oracion in oraciones:
        n = len(oracion.split())
        if palabras_actual + n > palabras_por_parte and actual:
            bloques.append(" ".join(actual))
            actual = []
            palabras_actual = 0
        actual.append(oracion)
        palabras_actual += n
    if actual:
        bloques.append(" ".join(actual))
    return bloques


def parafrasear(texto_crudo):
    if not GEMINI_API_KEY:
        log("⚠️  Sin GEMINI_API_KEY, se sube el subtítulo tal cual (sin parafrasear)")
        return texto_crudo

    bloques = _partir_en_bloques(texto_crudo)
    if len(bloques) > 1:
        log(f"Texto largo: se divide en {len(bloques)} parte(s) para parafrasear sin perder contenido")

    partes_finales = []
    for i, bloque in enumerate(bloques, start=1):
        if len(bloques) > 1:
            log(f"  Parte {i}/{len(bloques)}: parafraseando con Gemini...")
        else:
            log("Parafraseando con Gemini...")
        texto = _llamar_gemini(GEMINI_PROMPT_PARAFRASEAR, bloque)
        texto = _limpiar_artefactos(texto)
        partes_finales.append(texto.strip())
        if i < len(bloques):
            time.sleep(PAUSA_ENTRE_LLAMADAS_SEG)

    return " ".join(partes_finales)


def _extraer_nombre_canal(url):
    url = url.split("?")[0].strip().rstrip("/")
    return url


def es_link_de_video(url):
    return "watch?v=" in url or "youtu.be/" in url or "/shorts/" in url


COOKIES_PATH = "cookies.txt"


def _opts_cookies():
    if os.path.isfile(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0:
        return {"cookiefile": COOKIES_PATH}
    return {}


def _opts_base():
    return {
        "ignore_no_formats_error": True,
        "extractor_args": {"youtube": {"player_client": ["tv", "web"]}},
    }


def listar_videos_canal(url_canal, max_videos):
    url_videos = _extraer_nombre_canal(url_canal)
    if not url_videos.endswith("/videos"):
        url_videos += "/videos"
    opts = {
        "extract_flat": True,
        "playlistend": max_videos,
        "quiet": True,
        **_opts_base(),
        "extractor_args": {
            "youtubetab": {"skip": ["authcheck"]},
            **_opts_base()["extractor_args"],
        },
        **_opts_cookies(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url_videos, download=False)
    entradas = info.get("entries", []) or []
    videos = []
    for e in entradas:
        if not e:
            continue
        duracion = e.get("duration") or 0
        video_url = e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"
        if duracion and duracion <= 60:
            continue
        if "/shorts/" in video_url:
            continue
        videos.append({"id": e.get("id"), "title": e.get("title", "sin_titulo"), "url": video_url})
        if len(videos) >= max_videos:
            break
    return videos


def bajar_subtitulo(video_url):
    opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["es", "es-419", "es-ES"],
        "subtitlesformat": "vtt",
        "outtmpl": os.path.join(CARPETA_SALIDA, "%(id)s.%(ext)s"),
        "quiet": True,
        **_opts_base(),
        **_opts_cookies(),
    }
    ultimo_error = None
    for intento in range(1, 4):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
            video_id = info.get("id")
            for archivo in os.listdir(CARPETA_SALIDA):
                if archivo.startswith(video_id) and archivo.endswith(".vtt"):
                    ruta = os.path.join(CARPETA_SALIDA, archivo)
                    texto = _vtt_a_texto(ruta)
                    os.remove(ruta)
                    return texto
            return None
        except Exception as e:
            ultimo_error = e
            log(f"  ⚠️  Intento {intento}/3 falló: {e}")
            time.sleep(5 * intento)
    log(f"  ❌ No se pudo bajar el subtítulo: {ultimo_error}")
    return None


def _vtt_a_texto(ruta_vtt):
    with open(ruta_vtt, "r", encoding="utf-8") as f:
        lineas = f.readlines()
    vistas = set()
    partes = []
    for linea in lineas:
        linea = linea.strip()
        if not linea or "-->" in linea or linea.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        linea = re.sub(r"<[^>]+>", "", linea)
        if linea and linea not in vistas:
            vistas.add(linea)
            partes.append(linea)
    return " ".join(partes)


def _cliente_drive():
    creds = Credentials(
        None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds)


def _id_carpeta_drive(servicio, nombre_carpeta):
    q = (
        f"name='{nombre_carpeta}' and mimeType='application/vnd.google-apps.folder' "
        "and trashed=false"
    )
    resultado = servicio.files().list(q=q, fields="files(id)").execute()
    archivos = resultado.get("files", [])
    if archivos:
        return archivos[0]["id"]
    metadata = {"name": nombre_carpeta, "mimeType": "application/vnd.google-apps.folder"}
    carpeta = servicio.files().create(body=metadata, fields="id").execute()
    return carpeta["id"]


def subir_a_drive(nombre_archivo, texto):
    servicio = _cliente_drive()
    carpeta_id = _id_carpeta_drive(servicio, DRIVE_FOLDER_NAME)
    metadata = {"name": nombre_archivo, "parents": [carpeta_id]}
    media = MediaIoBaseUpload(io.BytesIO(texto.encode("utf-8")), mimetype="text/plain")
    servicio.files().create(body=metadata, media_body=media, fields="id").execute()
    log(f"  ☁️  Subido a Drive: {nombre_archivo}")


def _nombre_archivo_valido(titulo, video_id):
    limpio = re.sub(r"[^\w\s-]", "", titulo).strip().replace(" ", "_")[:80]
    return f"{limpio}__{video_id}.txt"


HISTORIAL_PATH = "descargados.txt"


def _cargar_historial():
    if not os.path.isfile(HISTORIAL_PATH):
        return set()
    with open(HISTORIAL_PATH, "r", encoding="utf-8") as f:
        return {linea.strip() for linea in f if linea.strip()}


def _marcar_como_descargado(video_id):
    with open(HISTORIAL_PATH, "a", encoding="utf-8") as f:
        f.write(f"{video_id}\n")


PARAFRASEAR = os.environ.get("PARAFRASEAR", "0").strip() == "1"


def main():
    if len(sys.argv) < 2:
        log("Uso: python procesar.py <canal_o_link>[,<canal2>,<canal3>,...] [max_videos]")
        sys.exit(1)
    entrada = sys.argv[1].strip()
    max_videos = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    canales = [c.strip() for c in entrada.split(",") if c.strip()]

    historial = _cargar_historial()
    log(f"Historial: {len(historial)} video(s) ya procesados antes")
    log("Modo: subir texto CRUDO (sin parafrasear)" if not PARAFRASEAR else "Modo: parafrasear con Gemini antes de subir")
    log(f"Canales en la lista: {len(canales)} — objetivo: {max_videos} video(s) en total")

    procesados = 0
    for idx_canal, canal_o_link in enumerate(canales, start=1):
        if procesados >= max_videos:
            break
        restantes = max_videos - procesados

        if es_link_de_video(canal_o_link):
            videos = [{"id": None, "title": "video", "url": canal_o_link}]
        else:
            log(f"[canal {idx_canal}/{len(canales)}] Listando videos recientes de: {canal_o_link}")
            videos = listar_videos_canal(canal_o_link, restantes * 3)
            videos = [v for v in videos if v["id"] not in historial][:restantes]

        if not videos:
            log("  Sin contenido nuevo en este canal. Se pasa al siguiente.")
            continue

        for i, video in enumerate(videos, start=1):
            log(f"--- Video {i}/{len(videos)} ({canal_o_link}): {video['title']} ---")
            texto_crudo = bajar_subtitulo(video["url"])
            if not texto_crudo:
                log("  Sin subtítulo disponible, se saltea.")
                continue
            if PARAFRASEAR:
                texto_final = parafrasear(texto_crudo)
            else:
                texto_final = texto_crudo
            video_id = video["id"] or "video"
            nombre_archivo = _nombre_archivo_valido(video["title"], video_id)
            ruta_local = os.path.join(CARPETA_SALIDA, nombre_archivo)
            with open(ruta_local, "w", encoding="utf-8") as f:
                f.write(texto_final)
            subir_a_drive(nombre_archivo, texto_final)
            if video["id"]:
                _marcar_como_descargado(video["id"])
            procesados += 1
            if procesados >= max_videos:
                break

    if procesados == 0:
        log("No hay videos nuevos para procesar en ningún canal de la lista.")
    else:
        log(f"Listo. {procesados} video(s) procesado(s) en total.")


if __name__ == "__main__":
    main()
