"""
yt-download — Descargar subtítulos, parafrasear con Gemini y subir a Drive
=============================================================================
Versión: 1.1

Pensado para correr dentro de GitHub Actions (workflow_dispatch), sin
depender del celular/Termux. Flujo:

  1. Recibe un canal o link de video de YouTube (por variable de entorno,
     pasada desde el input del workflow).
  2. Lista los videos recientes del canal (o usa el video puntual si se
     pasó un link directo) y baja el subtítulo automático en español de
     cada uno con yt-dlp (sin descargar audio ni video).
  3. Manda ese texto a Gemini para que lo parafrasee/corrija (misma
     lógica en dos pasadas que ya usa TRANSCRIBIR_WEB).
  4. Sube el .txt final a una carpeta de Google Drive, usando las
     credenciales OAuth ya generadas (refresh token reutilizado, no pide
     login en cada corrida).

Requiere estas variables de entorno (Secrets del repo en GitHub Actions):
    GDRIVE_CLIENT_ID
    GDRIVE_CLIENT_SECRET
    GDRIVE_REFRESH_TOKEN
    GEMINI_API_KEY

Y estos inputs del workflow (ver .github/workflows/procesar.yml):
    canal        -> link o @usuario del canal, o link directo de un video
    max_videos   -> cuántos videos nuevos procesar en esta corrida

Uso local (para probar):
    export GDRIVE_CLIENT_ID=...
    export GDRIVE_CLIENT_SECRET=...
    export GDRIVE_REFRESH_TOKEN=...
    export GEMINI_API_KEY=...
    python procesar.py "https://www.youtube.com/@canal" 3
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

# --- Config -----------------------------------------------------------
CARPETA_SALIDA = "transcripciones"
os.makedirs(CARPETA_SALIDA, exist_ok=True)

DRIVE_FOLDER_NAME = "yt-download"  # carpeta en Drive donde se sube todo

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELO = os.environ.get("GEMINI_MODELO", "gemini-3.6-flash").strip()
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELO}:generateContent"
)
GEMINI_TIMEOUT_SEG = 240

GEMINI_PROMPT_GENERAR = """Sos un editor de guiones en español. Te paso el subtítulo automático de un video de YouTube, que puede tener palabras mal reconocidas o mal puntuadas.

Tu tarea:
1. Corregí los errores de reconocimiento (palabras que claramente están mal, cortadas o repetidas por ser subtítulo automático).
2. Reescribí el texto cambiando palabras (sinónimos) y la estructura de las oraciones, manteniendo EXACTAMENTE la misma historia, los mismos hechos y el mismo sentido. No inventes ni agregues información nueva.
3. Puntuá el texto pensando en que lo va a leer una voz sintética: usá comas para pausas cortas, puntos entre ideas, y evitá oraciones de más de 20 palabras.
4. Devolvé SOLO el guion final, sin comentarios, sin explicaciones, sin encabezados.

Subtítulo original:
{texto}"""

GEMINI_PROMPT_REVISAR = """Sos un editor revisando un guion que vos mismo reescribiste. Releélo con ojo crítico y fijate si quedó algo raro: frases repetidas, puntuación que no ayuda a una lectura natural en voz alta, o algo que se haya alejado del sentido original. Corregí lo que haga falta.

Devolvé SOLO la versión final pulida del guion, sin comentarios ni explicaciones.

Guion a revisar:
{texto}"""


def log(msg):
    print(f"[yt-download] {msg}", flush=True)


# --- Gemini: parafraseo en dos pasadas (misma lógica que TRANSCRIBIR_WEB) --
def _llamar_gemini(prompt_template, texto):
    if not GEMINI_API_KEY:
        return texto
    body = {"contents": [{"parts": [{"text": prompt_template.format(texto=texto)}]}]}
    resp = requests.post(
        f"{GEMINI_URL}?key={GEMINI_API_KEY}",
        json=body,
        timeout=GEMINI_TIMEOUT_SEG,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def parafrasear(texto_crudo):
    if not GEMINI_API_KEY:
        log("⚠️  Sin GEMINI_API_KEY, se sube el subtítulo tal cual (sin parafrasear)")
        return texto_crudo
    log("Parafraseando con Gemini (pasada 1/2)...")
    texto = _llamar_gemini(GEMINI_PROMPT_GENERAR, texto_crudo)
    log("Puliendo con Gemini (pasada 2/2)...")
    texto = _llamar_gemini(GEMINI_PROMPT_REVISAR, texto)
    return texto


# --- yt-dlp: listar videos y bajar subtítulo automático -----------------
def _extraer_nombre_canal(url):
    url = url.split("?")[0].strip().rstrip("/")
    return url


def es_link_de_video(url):
    return "watch?v=" in url or "youtu.be/" in url or "/shorts/" in url


COOKIES_PATH = "cookies.txt"


def _opts_cookies():
    """Si existe cookies.txt (generado por el workflow) y no está vacío,
    lo agrega a las opciones de yt-dlp para evitar bloqueos por
    age-restriction o límites de YouTube. Si no existe, no rompe nada."""
    if os.path.isfile(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0:
        return {"cookiefile": COOKIES_PATH}
    return {}


def listar_videos_canal(url_canal, max_videos):
    url_videos = _extraer_nombre_canal(url_canal)
    if not url_videos.endswith("/videos"):
        url_videos += "/videos"
    opts = {
        "extract_flat": True,
        "playlistend": max_videos,
        "quiet": True,
        "extractor_args": {"youtubetab": {"skip": ["authcheck"]}},
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
            continue  # descarta Shorts
        if "/shorts/" in video_url:
            continue
        videos.append({"id": e.get("id"), "title": e.get("title", "sin_titulo"), "url": video_url})
        if len(videos) >= max_videos:
            break
    return videos


def bajar_subtitulo(video_url):
    """Baja el subtítulo automático en español (sin audio/video) y
    devuelve el texto plano. Reintenta ante fallas transitorias."""
    opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["es", "es-419", "es-ES"],
        "subtitlesformat": "vtt",
        "outtmpl": os.path.join(CARPETA_SALIDA, "%(id)s.%(ext)s"),
        "quiet": True,
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
            return None  # no había subtítulo disponible
        except Exception as e:
            ultimo_error = e
            log(f"  ⚠️  Intento {intento}/3 falló: {e}")
            time.sleep(5 * intento)
    log(f"  ❌ No se pudo bajar el subtítulo: {ultimo_error}")
    return None


def _vtt_a_texto(ruta_vtt):
    """Convierte un .vtt en texto plano, sin timestamps ni duplicados
    de línea (típico de los subtítulos automáticos de YouTube)."""
    with open(ruta_vtt, "r", encoding="utf-8") as f:
        lineas = f.readlines()
    vistas = set()
    partes = []
    for linea in lineas:
        linea = linea.strip()
        if not linea or "-->" in linea or linea.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        linea = re.sub(r"<[^>]+>", "", linea)  # saca tags de estilo
        if linea and linea not in vistas:
            vistas.add(linea)
            partes.append(linea)
    return " ".join(partes)


# --- Google Drive: subir el .txt final -----------------------------------
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


# --- Main ------------------------------------------------------------
def _nombre_archivo_valido(titulo, video_id):
    limpio = re.sub(r"[^\w\s-]", "", titulo).strip().replace(" ", "_")[:80]
    return f"{limpio}__{video_id}.txt"


def main():
    if len(sys.argv) < 2:
        log("Uso: python procesar.py <canal_o_link> [max_videos]")
        sys.exit(1)
    canal_o_link = sys.argv[1].strip()
    max_videos = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    if es_link_de_video(canal_o_link):
        videos = [{"id": None, "title": "video", "url": canal_o_link}]
    else:
        log(f"Listando hasta {max_videos} video(s) recientes de: {canal_o_link}")
        videos = listar_videos_canal(canal_o_link, max_videos)

    if not videos:
        log("No se encontraron videos para procesar.")
        return

    for i, video in enumerate(videos, start=1):
        log(f"--- Video {i}/{len(videos)}: {video['title']} ---")
        texto_crudo = bajar_subtitulo(video["url"])
        if not texto_crudo:
            log("  Sin subtítulo disponible, se saltea.")
            continue
        texto_final = parafrasear(texto_crudo)
        video_id = video["id"] or "video"
        nombre_archivo = _nombre_archivo_valido(video["title"], video_id)
        ruta_local = os.path.join(CARPETA_SALIDA, nombre_archivo)
        with open(ruta_local, "w", encoding="utf-8") as f:
            f.write(texto_final)
        subir_a_drive(nombre_archivo, texto_final)

    log("Listo.")


if __name__ == "__main__":
    main()
