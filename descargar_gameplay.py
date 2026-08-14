name: Descargar Gameplay

on:
  workflow_dispatch:
    inputs:
      canal:
        description: "Canal de gameplay a descargar"
        required: true
        type: choice
        options:
          - "https://www.youtube.com/@OrbitalNCG"
          - "https://www.youtube.com/@Naybr"
      max_videos:
        description: "Cantidad de videos"
        required: false
        default: "1"
        type: string

permissions:
  contents: write

jobs:
  descargar:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - name: Descargar repo
        uses: actions/checkout@v4

      - name: Configurar Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Instalar dependencias
        run: pip install yt-dlp google-api-python-client google-auth-httplib2 google-auth-oauthlib
        
      - name: Preparar cookies
        run: |
          echo "# Netscape HTTP Cookie File" > cookies.txt
          echo "${{ secrets.YT_COOKIES }}" >> cookies.txt
          
      - name: Descargar video
        env:
          GDRIVE_CLIENT_ID: ${{ secrets.GDRIVE_CLIENT_ID }}
          GDRIVE_CLIENT_SECRET: ${{ secrets.GDRIVE_CLIENT_SECRET }}
          GDRIVE_REFRESH_TOKEN: ${{ secrets.GDRIVE_REFRESH_TOKEN }}
        run: python descargar_gameplay.py "${{ inputs.canal }}" "${{ inputs.max_videos }}"
        
      - name: Guardar historial
        run: |
          git config --global user.name "GitHub Actions Bot"
          git config --global user.email "actions@github.com"
          touch descargados_gameplay.txt
          git add descargados_gameplay.txt
          git diff --quiet && git diff --staged --quiet || (git commit -m "Actualizar historial gameplay" && git push)
