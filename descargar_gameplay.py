import os
import sys
import glob
import yt_dlp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

CANAL = sys.argv[1]
MAX_VIDEOS = int(sys.argv[2])
DRIVE_FOLDER_NAME = "yt-download"

def subir_a_drive(ruta_archivo):
    print(f"Subiendo a Drive: {ruta_archivo}")
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token"
    )
    service = build('drive', 'v3', credentials=creds)
    
    # Busca la carpeta en Drive
    results = service.files().list(
        q=f"name='{DRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        spaces='drive',
        fields='files(id, name)'
    ).execute()
    
    items = results.get('files', [])
    if not items:
        folder_metadata = {'name': DRIVE_FOLDER_NAME, 'mimeType': 'application/vnd.google-apps.folder'}
        folder = service.files().create(body=folder_metadata, fields='id').execute()
        folder_id = folder.get('id')
    else:
        folder_id = items[0].get('id')

    # Sube el video
    nombre_archivo = os.path.basename(ruta_archivo)
    file_metadata = {'name': nombre_archivo, 'parents': [folder_id]}
    media = MediaFileUpload(ruta_archivo, mimetype='video/mp4', resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    print("✅ Subida exitosa a Drive.")

def procesar_gameplay():
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': '%(title)s.%(ext)s',
        'cookiefile': 'cookies.txt',
        'download_archive': 'descargados_gameplay.txt',
        'playlistend': MAX_VIDEOS,
        'extractor_args': {'youtube': {'player_client': ['tv', 'android', 'web']}},
    }

    print(f"Iniciando descarga de {CANAL} en máxima calidad...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([CANAL])

    # Busca los mp4 descargados y los sube a Drive
    for archivo in glob.glob("*.mp4"):
        subir_a_drive(archivo)
        os.remove(archivo) # Limpia el servidor al terminar

if __name__ == "__main__":
    if not CANAL:
        print("Falta indicar el canal.")
        sys.exit(1)
    procesar_gameplay()
