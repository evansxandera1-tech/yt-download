"""
yt-download — Descargar subtítulos, parafrasear con Groq y subir a Drive
=============================================================================
Versión: 2.3

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
  4. Parafrasea (si PARAFRASEAR=1) con Groq (respaldo Gemini) y sube el
     .txt final a una carpeta de Google Drive vía rclone.
  5. Guarda el ID de cada video procesado en descargados.txt para no
     repetirlo en corridas futuras.

Requiere estas variables de entorno (Secrets del repo en GitHub Actions):
    GROQ_API_KEY          (principal para parafrasear, si PARAFRASEAR=1)
    GROQ_API_KEY_2        (respaldo, opcional)
    GEMINI_API_KEY        (respaldo si Groq falla, opcional)

Y que rclone ya esté configurado en el runner (remote "gdrive:", ver el
paso "Configurar rclone" del workflow), en vez de pedir client_id/
client_secret/refresh_token de Google directamente.

v2.2: el parafraseo (cuando PARAFRASEAR=1) ahora usa Groq (Llama 3.3
70B) como motor principal, igual que paraphrase_engine.py, en vez de
Gemini. Gemini queda como respaldo si Groq falla (las dos keys) o si
no hay GROQ_API_KEY cargada. Mismo prompt de corrección/parafraseo de
antes, sin cambios en el criterio de reescritura.

v2.3: se reemplazó la subida a Drive con la librería de Google
(google-auth + googleapiclient), que dependía de un refresh token que
se venció/revocó (RefreshError: invalid_grant), por rclone (comando
"rclone copy"), reutilizando el mismo remote "gdrive:" ya autenticado
que usa gameplay_slither.py. Ya no se necesitan los secrets
GDRIVE_CLIENT_ID/GDRIVE_CLIENT_SECRET/GDRIVE_REFRESH_TOKEN.
"""

import os
import re
import subprocess
import sys
import time

import requests
import yt_dlp

CARPETA_SALIDA = "transcripciones"
os.makedirs(CARPETA_SALIDA, exist_ok=True)

# Carpeta destino en Drive, dentro del remote "gdrive:" de rclone
# (mismo remote que ya usa gameplay_slither.py).
DRIVE_REMOTE_PATH = "gdrive:yt-download"

# ---------- Groq (motor principal de parafraseo) ----------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_API_KEY_2 = os.environ.get("GROQ_API_KEY_2", "").strip()
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_REINTENTOS_GROQ = 3

# ---------- Gemini (respaldo, solo si Groq falla o no hay key) ----------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELO = os.environ.get("GEMINI_MODELO", "gemini-3.6-flash").strip()
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELO}:generateContent"
)
GEMINI_TIMEOUT_SEG = 240

PROMPT_PARAFRASEAR = """Sos un editor de guiones en español. Te paso el subtítulo automático de un video de YouTube, que puede tener palabras mal reconocidas o mal puntuadas.

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


def _llamar_groq(texto, api_key):
    body = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": PROMPT_PARAFRASEAR.format(texto=texto)}],
        "temperature": 0.7,
    }
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
    except requests.exceptions.RequestException as e:
        log(f"  ⚠️  Error de red llamando a Groq: {e}")
        return None
    if resp.status_code == 200:
        return resp.json()["choices"][0]["message"]["content"]
    log(f"  ⚠️  Groq error {resp.status_code}: {resp.text[:200]}")
    return None


def _llamar_gemini(texto):
    if not GEMINI_API_KEY:
        return None
    body = {
        "contents": [{"parts": [{"text": PROMPT_PARAFRASEAR.format(texto=texto)}]}],
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
        log(f"  ❌ Gemini no respondió tras varios intentos ({ultimo_error}).")
        return None

    candidatos = data.get("candidates") or []
    if not candidatos:
        motivo = data.get("promptFeedback", {}).get("blockReason", "desconocido")
        log(f"  ⚠️  Gemini no devolvió candidatos (motivo: {motivo}).")
        return None

    candidato = candidatos[0]
    finish_reason = candidato.get("finishReason", "")
    if finish_reason == "MAX_TOKENS":
        log("  ⚠️  Gemini cortó la respuesta por límite de tokens de salida (MAX_TOKENS). El bloque puede haber quedado incompleto.")

    partes = candidato.get("content", {}).get("parts")
    if not partes:
        motivo = finish_reason or "desconocido"
        log(f"  ⚠️  Gemini devolvió una respuesta sin contenido (finishReason: {motivo}).")
        return None

    return partes[0].get("text", "").strip()


def _corregir_bloque(bloque):
    """Groq (con las 2 keys, con reintentos) primero; Gemini de respaldo si todo falla."""
    for api_key in [k for k in (GROQ_API_KEY, GROQ_API_KEY_2) if k]:
        for intento in range(1, MAX_REINTENTOS_GROQ + 1):
            resultado = _llamar_groq(bloque, api_key)
            if resultado and resultado.strip():
                return _limpiar_artefactos(resultado)
            log(f"  ⏳ Groq intento {intento}/{MAX_REINTENTOS_GROQ} sin resultado válido, reintentando...")
            time.sleep(3)

    log("  ⚠️  Groq falló (o sin GROQ_API_KEY), probando con Gemini de respaldo...")
    resultado = _llamar_gemini(bloque)
    if resultado and resultado.strip():
        return _limpiar_artefactos(resultado)

    log("  ❌ Groq y Gemini fallaron para este bloque. Se usa el texto sin cambios.")
    return bloque


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
    if not GROQ_API_KEY and not GEMINI_API_KEY:
        log("⚠️  Sin GROQ_API_KEY ni GEMINI_API_KEY, se sube el subtítulo tal cual (sin parafrasear)")
        return texto_crudo

    bloques = _partir_en_bloques(texto_crudo)
    if len(bloques) > 1:
        log(f"Texto largo: se divide en {len(bloques)} parte(s) para parafrasear sin perder contenido")

    partes_finales = []
    for i, bloque in enumerate(bloques, start=1):
        if len(bloques) > 1:
            log(f"  Parte {i}/{len(bloques)}: parafraseando...")
        else:
            log("Parafraseando...")
        texto = _corregir_bloque(bloque)
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
    for langs in (["es", "es-419", "es-ES"], ["en"]):
        opts = {
            "skip_download": True,
            "writeautomaticsub": True,
            "subtitleslangs": langs,
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
                ultimo_error = None
                break
            except Exception as e:
                ultimo_error = e
                log(f"  ⚠️  Intento {intento}/3 falló ({langs}): {e}")
                time.sleep(5 * intento)
        if ultimo_error:
            log(f"  ❌ No se pudo bajar el subtítulo en {langs}: {ultimo_error}")
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


def subir_a_drive(nombre_archivo, texto):
    """Sube el .txt a Drive vía rclone (remote 'gdrive:'), en vez de la
    API de Google directamente. Requiere rclone ya configurado en el
    runner (ver paso 'Configurar rclone' del workflow)."""
    ruta_local = os.path.join(CARPETA_SALIDA, nombre_archivo)
    resultado = subprocess.run(
        ["rclone", "copy", ruta_local, DRIVE_REMOTE_PATH, "-v"],
        capture_output=True,
        text=True,
    )
    if resultado.returncode != 0:
        log(f"  ❌ rclone falló subiendo {nombre_archivo}: {resultado.stderr.strip()[:300]}")
        raise RuntimeError(f"rclone copy falló para {nombre_archivo}")
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
    log("Modo: subir texto CRUDO (sin parafrasear)" if not PARAFRASEAR else "Modo: parafrasear (Groq, respaldo Gemini) antes de subir")
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
