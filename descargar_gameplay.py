"""
descargar_gameplay.py — v1.4

Descarga gameplay "sin copy" de canales de YouTube para usar como fondo
de video, pensado para correr en GitHub Actions (sin depender del
celular). Cada video se sube a Google Drive apenas termina de
descargarse y se borra del runner al toque, porque el runner no tiene
almacenamiento permanente.

FLUJO:
  1) Baja historial_gameplay.json desde Drive (si existe).
  2) Recorre los canales en CANALES, uno a la vez (vacía el canal
     completo antes de pasar al siguiente).
  3) Por cada video: si ya está "completo" en el historial, lo salta.
     Si no, lo marca "en_progreso" y sube el historial YA (para que,
     si el run se corta a la mitad, la próxima corrida sepa que ese
     video quedó a medias y lo vuelva a intentar).
  4) Descarga el video en máximo 1080p, lo clasifica por orientación
     (vertical/horizontal) y lo sube a la subcarpeta correspondiente
     en Drive, dentro de la carpeta del canal.
  5) Marca el video "completo" en el historial y lo vuelve a subir.
  6) Después de cada subida, si el total guardado en Drive supera
     ALMACENAMIENTO_MAXIMO_BYTES, borra el/los video(s) más viejo(s)
     ya completados hasta volver a estar bajo el tope (rotación).
  7) Corta la corrida cuando lo descargado en ESTA corrida llega a
     TOPE_BYTES_POR_CORRIDA (para no pasarse del tiempo de GitHub
     Actions).

SECRETS necesarios en el repo de GitHub:
  DRIVE_CLIENT_ID
  DRIVE_CLIENT_SECRET
  DRIVE_REFRESH_TOKEN
  DRIVE_FOLDER_ID     (carpeta raíz en Drive donde se guarda todo)
  YOUTUBE_COOKIES     (contenido del cookies.txt exportado del navegador,
                       para que yt-dlp no choque con el bloqueo
                       "Sign in to confirm you're not a bot")

v1.0: primera versión, separada del módulo de gameplay de
transcribir_web.py, adaptada para correr en GitHub Actions + Drive
en vez de Termux + almacenamiento local.

v1.1: ahora chequea la orientación del video ANTES de descargarlo
(sin gastar tiempo/datos en algo que se iba a descartar) y solo
guarda horizontales; los verticales se marcan "omitido_vertical" en
el historial para no volver a chequearlos. Se sacó la subcarpeta
vertical/horizontal en Drive ya que todo lo guardado es horizontal.

v1.2: los videos estaban llegando a Drive en baja calidad porque sin
un PO Token válido YouTube le entrega a yt-dlp solo formatos de baja
resolución (a veces 360p) para los clients tv/android/web. Se agregó
soporte para un servidor bgutil-ytdlp-pot-provider (corre como
service del workflow, puerto 4416) que genera el PO Token y permite
acceder a los formatos de hasta 1080p.

v1.3: el tope de 12 GB se estaba entendiendo mal: con solo 60 videos
recientes por canal como candidatos, en una o pocas corridas se
agotaba el pool completo (quedaba todo "completo" en el historial) y
las corridas siguientes ya no tenían nada nuevo para bajar, aunque
Drive no estuviera lleno de verdad. Se cambia el enfoque: ahora Drive
funciona como un buffer rotativo de tamaño fijo (ALMACENAMIENTO_MAXIMO_
BYTES). Cuando se sube un video nuevo y esto hace que el total
guardado supere el tope, se borra el video más viejo ya completado
(de Drive y del historial) para hacerle lugar. Así, mientras el canal
siga subiendo contenido nuevo, el script siempre tiene dónde
descargar, sin que el Drive crezca sin control.

v1.4: YouTube empezó a forzar "SABR streaming", lo que rompe la
mayoría de los formatos que pedía yt-dlp incluso con el PO Token del
bgutil-provider funcionando bien (error "Requested format is not
available" en casi todos los videos). Se agregó el argumento
extractor_args["youtube"]["formats"] = ["missing_pot"], que le
permite a yt-dlp usar formatos aunque falte el PO token en vez de
descartarlos de plano.
"""

import io
import json
import os
import sys
import time
import traceback

import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

# --------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------

CANALES = [
    "https://youtube.com/@orbitalncg",
    "https://youtube.com/@dopegameplays",
]

CALIDAD_MAXIMA = 1080  # alto máximo en píxeles

# Cuánto se descarga como máximo EN UNA SOLA CORRIDA (para no pasarse
# del tiempo/recursos de GitHub Actions). No es el tope de Drive.
TOPE_BYTES_POR_CORRIDA = 12 * 1024 ** 3  # 12 GB por corrida

# Cuánto gameplay se mantiene guardado en Drive EN TOTAL. Si al subir
# un video nuevo se supera esto, se borra el más viejo para hacerle
# lugar (rotación). Podés subir este número si querés más backlog
# disponible para Story Engine.
ALMACENAMIENTO_MAXIMO_BYTES = 12 * 1024 ** 3  # 12 GB totales en Drive

LIMITE_VIDEOS_POR_CANAL = 60  # cuántos videos recientes revisa por canal

HISTORIAL_NOMBRE = "historial_gameplay.json"
COOKIES_PATH = os.path.expanduser("~/cookies.txt")
CARPETA_TEMP = os.path.expanduser("~/gameplay_dl_temp")

DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]


# --------------------------------------------------------------------
# LOG
# --------------------------------------------------------------------

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------
# DRIVE
# --------------------------------------------------------------------

def conectar_drive():
    creds = Credentials(
        None,
        refresh_token=os.environ["DRIVE_REFRESH_TOKEN"],
        client_id=os.environ["DRIVE_CLIENT_ID"],
        client_secret=os.environ["DRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds)


def _buscar_archivo(drive, nombre, carpeta_padre_id):
    query = (
        f"name='{nombre}' and '{carpeta_padre_id}' in parents "
        f"and trashed=false"
    )
    resultado = drive.files().list(q=query, fields="files(id,name)").execute()
    archivos = resultado.get("files", [])
    return archivos[0]["id"] if archivos else None


def obtener_o_crear_carpeta(drive, nombre, carpeta_padre_id):
    query = (
        f"name='{nombre}' and '{carpeta_padre_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    resultado = drive.files().list(q=query, fields="files(id,name)").execute()
    archivos = resultado.get("files", [])
    if archivos:
        return archivos[0]["id"]
    metadata = {
        "name": nombre,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [carpeta_padre_id],
    }
    carpeta = drive.files().create(body=metadata, fields="id").execute()
    return carpeta["id"]


def descargar_historial(drive):
    file_id = _buscar_archivo(drive, HISTORIAL_NOMBRE, DRIVE_FOLDER_ID)
    if not file_id:
        log("No hay historial previo en Drive, arranca uno nuevo.")
        return {}, None

    request = drive.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    listo = False
    while not listo:
        _, listo = downloader.next_chunk()
    buffer.seek(0)
    historial = json.loads(buffer.read().decode("utf-8"))

    en_progreso = [v for v, d in historial.items() if d.get("estado") == "en_progreso"]
    if en_progreso:
        log(f"{len(en_progreso)} video(s) habían quedado a medias, se reintentan.")
    log(f"Historial cargado desde Drive: {len(historial)} video(s) registrados.")
    return historial, file_id


def subir_historial(drive, historial, file_id):
    contenido = json.dumps(historial, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(contenido), mimetype="application/json")
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        metadata = {"name": HISTORIAL_NOMBRE, "parents": [DRIVE_FOLDER_ID]}
        creado = drive.files().create(body=metadata, media_body=media, fields="id").execute()
        file_id = creado["id"]
    return file_id


def subir_video(drive, ruta_local, nombre_archivo, carpeta_destino_id):
    metadata = {"name": nombre_archivo, "parents": [carpeta_destino_id]}
    media = MediaFileUpload(ruta_local, mimetype="video/mp4", resumable=True)
    creado = drive.files().create(body=metadata, media_body=media, fields="id").execute()
    return creado["id"]


def borrar_video_drive(drive, file_id):
    try:
        drive.files().delete(fileId=file_id).execute()
        return True
    except Exception as e:
        log(f"    ⚠️ No se pudo borrar de Drive ({file_id}): {e}")
        return False


# --------------------------------------------------------------------
# ROTACIÓN DE ESPACIO
# --------------------------------------------------------------------

def _total_guardado_bytes(historial):
    total = 0
    for datos in historial.values():
        if datos.get("estado") == "completo":
            total += datos.get("tamano_mb", 0) * 1024 * 1024
    return total


def liberar_espacio(drive, historial, historial_id, presupuesto_bytes):
    """Si lo guardado en Drive supera presupuesto_bytes, borra los
    videos más viejos ya completados (en orden de finalización) hasta
    volver a estar por debajo del tope."""
    total = _total_guardado_bytes(historial)
    if total <= presupuesto_bytes:
        return historial_id

    # Recorre el historial en el orden en que fue quedando "completo"
    # (los dicts en Python respetan el orden de inserción).
    for video_id, datos in list(historial.items()):
        if total <= presupuesto_bytes:
            break
        if datos.get("estado") != "completo":
            continue

        file_id = datos.get("drive_file_id")
        titulo = datos.get("titulo", video_id)
        if file_id and borrar_video_drive(drive, file_id):
            total -= datos.get("tamano_mb", 0) * 1024 * 1024
            historial[video_id]["estado"] = "borrado_por_espacio"
            historial[video_id].pop("drive_file_id", None)
            log(f"    🗑️ Borrado por rotación (el más viejo): {titulo}")

    return subir_historial(drive, historial, historial_id)


# --------------------------------------------------------------------
# YT-DLP
# --------------------------------------------------------------------

def _opciones_comunes():
    opciones = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "android", "web"],
                "formats": ["missing_pot"],
            },
            "youtubepot-bgutilhttp": {"base_url": "http://127.0.0.1:4416"},
        },
        "retries": 5,
        "fragment_retries": 5,
        "sleep_interval_requests": 1,
    }
    if os.path.exists(COOKIES_PATH):
        opciones["cookiefile"] = COOKIES_PATH
    return opciones


def listar_videos_canal(url_canal, limite):
    url_videos = url_canal.rstrip("/")
    if not url_videos.endswith("/videos"):
        url_videos += "/videos"

    opciones = _opciones_comunes()
    opciones.update({
        "extract_flat": True,
        "playlistend": limite,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "android", "web"],
                "formats": ["missing_pot"],
            },
            "youtubetab": {"skip": ["authcheck"]},
        },
    })

    with yt_dlp.YoutubeDL(opciones) as ydl:
        info = ydl.extract_info(url_videos, download=False)

    videos = []
    for entrada in (info or {}).get("entries") or []:
        url_entrada = entrada.get("url") or entrada.get("webpage_url") or ""
        if "/shorts/" in url_entrada:
            continue
        duracion = entrada.get("duration")
        if duracion is not None and duracion <= 60:
            continue
        video_id = entrada.get("id")
        if not video_id:
            continue
        videos.append({"video_id": video_id, "titulo": entrada.get("title") or video_id})
    return videos


def obtener_dimensiones(video_id):
    """Consulta metadata del video (sin descargarlo) para saber si es
    horizontal o vertical antes de gastar tiempo/datos bajándolo."""
    opciones = _opciones_comunes()
    opciones.update({"skip_download": True})
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opciones) as ydl:
        info = ydl.extract_info(url, download=False)
    return info.get("width"), info.get("height")


def descargar_video(video_id, nombre_salida):
    # bestaudio = se baja el audio original tal cual lo tiene YouTube, sin
    # tocarlo. Para el video, format_sort prioriza primero la resolución
    # (1080p tope) y después el codec más eficiente (av1/vp9 pesan menos
    # que h264 para la misma nitidez); si el canal no tiene esos codecs,
    # cae a lo que haya disponible en esa resolución.
    formato = f"bestvideo[height<={CALIDAD_MAXIMA}]+bestaudio/best[height<={CALIDAD_MAXIMA}]/best"
    opciones = _opciones_comunes()
    opciones.update({
        "format": formato,
        "format_sort": ["res:1080", "vcodec:av01:vp9.2:vp9", "br"],
        "merge_output_format": "mp4",
        "outtmpl": nombre_salida,
    })
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opciones) as ydl:
        info = ydl.extract_info(url, download=True)
    return info.get("width"), info.get("height")


def _sanear_nombre_archivo(texto, largo_max=80):
    limpio = "".join(c for c in texto if c.isalnum() or c in " -_").strip()
    return limpio[:largo_max] or "video"


# --------------------------------------------------------------------
# PRINCIPAL
# --------------------------------------------------------------------

def main():
    os.makedirs(CARPETA_TEMP, exist_ok=True)
    drive = conectar_drive()
    historial, historial_id = descargar_historial(drive)

    limite_prueba = os.environ.get("LIMITE_VIDEOS_PRUEBA")
    limite_prueba = int(limite_prueba) if limite_prueba else None
    if limite_prueba:
        log(f"MODO PRUEBA: se corta después de {limite_prueba} video(s).")

    total_bytes_corrida = 0
    videos_descargados = 0
    tope_alcanzado = False

    for url_canal in CANALES:
        if tope_alcanzado:
            break

        nombre_canal = url_canal.rstrip("/").split("/")[-1].lstrip("@")
        log(f"=== Canal: {nombre_canal} ===")

        try:
            videos = listar_videos_canal(url_canal, LIMITE_VIDEOS_POR_CANAL)
        except Exception as e:
            log(f"  ❌ No se pudo listar el canal: {type(e).__name__}: {e}")
            continue

        carpeta_canal_id = obtener_o_crear_carpeta(drive, nombre_canal, DRIVE_FOLDER_ID)

        for video in videos:
            if tope_alcanzado:
                break

            video_id = video["video_id"]
            titulo = video["titulo"]
            registro = historial.get(video_id)

            # "borrado_por_espacio" también se salta: ya se usó una vez,
            # no lo volvemos a bajar en la misma corrida/ventana de 60.
            if registro and registro.get("estado") in (
                "completo", "omitido_vertical", "borrado_por_espacio"
            ):
                continue

            try:
                ancho, alto = obtener_dimensiones(video_id)
            except Exception as e:
                log(f"  ⚠️ No se pudo chequear orientación de '{titulo}': {e}")
                continue

            if ancho and alto and alto > ancho:
                log(f"  ⏭️ Saltado (vertical): {titulo}")
                historial[video_id] = {
                    "canal": nombre_canal,
                    "titulo": titulo,
                    "estado": "omitido_vertical",
                }
                historial_id = subir_historial(drive, historial, historial_id)
                continue

            log(f"  → {titulo}")
            historial[video_id] = {
                "canal": nombre_canal,
                "titulo": titulo,
                "estado": "en_progreso",
            }
            historial_id = subir_historial(drive, historial, historial_id)

            ruta_temp = os.path.join(CARPETA_TEMP, f"{video_id}.mp4")
            try:
                descargar_video(video_id, ruta_temp)
                if not os.path.exists(ruta_temp):
                    raise RuntimeError("la descarga no generó el archivo esperado")

                tamano_bytes = os.path.getsize(ruta_temp)
                nombre_final = f"{video_id}__{_sanear_nombre_archivo(titulo)}.mp4"

                log(f"    Subiendo a Drive ({round(tamano_bytes / (1024 * 1024), 1)} MB)...")
                drive_file_id = subir_video(drive, ruta_temp, nombre_final, carpeta_canal_id)

                historial[video_id]["estado"] = "completo"
                historial[video_id]["tamano_mb"] = round(tamano_bytes / (1024 * 1024), 1)
                historial[video_id]["drive_file_id"] = drive_file_id
                historial_id = subir_historial(drive, historial, historial_id)

                total_bytes_corrida += tamano_bytes
                videos_descargados += 1
                log(f"    ✅ Listo ({nombre_canal}). "
                    f"Acumulado esta corrida: {round(total_bytes_corrida / (1024 ** 3), 2)} GB")

                # Rotación: si con este video se pasó el tope total de
                # Drive, borra el/los más viejo(s) para hacerle lugar.
                historial_id = liberar_espacio(
                    drive, historial, historial_id, ALMACENAMIENTO_MAXIMO_BYTES
                )

            except Exception as e:
                log(f"    ❌ Error: {type(e).__name__}: {e}")
                traceback.print_exc()
                # Se deja "en_progreso" en el historial a propósito: la
                # próxima corrida lo va a reintentar.
            finally:
                if os.path.exists(ruta_temp):
                    os.remove(ruta_temp)

            if total_bytes_corrida >= TOPE_BYTES_POR_CORRIDA:
                log(f"Tope de {TOPE_BYTES_POR_CORRIDA / (1024 ** 3):.0f} GB por corrida "
                    f"alcanzado, se corta la corrida.")
                tope_alcanzado = True
            elif limite_prueba and videos_descargados >= limite_prueba:
                log("Límite de prueba alcanzado, se corta la corrida.")
                tope_alcanzado = True

    log("=== Fin de la corrida ===")


if __name__ == "__main__":
    main()
