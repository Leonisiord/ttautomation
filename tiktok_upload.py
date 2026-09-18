"""
TikTok — Subida automática (Content Posting API)
====================================================
Usa los tokens generados por tiktok_auth.py (ejecútalo primero una vez).

Dos modos disponibles:
  - upload_video_as_draft(...)   → sube a la bandeja de borradores. No admite
                                    título/descripción/hashtags por API; hay
                                    que rellenarlos a mano en el móvil.
  - upload_video_direct_post(...) → publica directamente con título y
                                     hashtags ya puestos. Mientras la app no
                                     esté auditada, TikTok obliga a que quede
                                     en privado (SELF_ONLY) — hay que abrir la
                                     app y cambiarlo a público a mano.
"""

import os
import json
import math
import requests
from dotenv import load_dotenv

load_dotenv()
CLIENT_KEY    = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
TOKENS_FILE   = "tiktok_tokens.json"

# Límites de TikTok para FILE_UPLOAD: cada trozo debe medir entre 5MB y 64MB
# (el último trozo puede llegar hasta 128MB). Un vídeo de menos de 5MB se
# manda entero como un único trozo; si pesa más de 64MB, hay que trocearlo.
_MIN_CHUNK = 5 * 1024 * 1024
_MAX_CHUNK = 60 * 1024 * 1024  # algo por debajo del límite de 64MB, con margen


def _compute_chunk_plan(video_size: int) -> tuple:
    """Devuelve (chunk_size, total_chunk_count) válidos para TikTok."""
    if video_size <= _MIN_CHUNK:
        return video_size, 1
    total_chunk_count = math.ceil(video_size / _MAX_CHUNK)
    chunk_size = math.ceil(video_size / total_chunk_count)
    return chunk_size, total_chunk_count


def _put_video_chunks(upload_url: str, video_path: str, video_size: int, chunk_size: int, total_chunk_count: int):
    """Sube el vídeo en uno o varios trozos, con el Content-Range correcto en cada uno."""
    with open(video_path, "rb") as f:
        for i in range(total_chunk_count):
            start = i * chunk_size
            end = min(start + chunk_size, video_size) - 1
            f.seek(start)
            chunk_bytes = f.read(end - start + 1)

            put_response = requests.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {start}-{end}/{video_size}",
                },
                data=chunk_bytes,
            )
            if not put_response.ok:
                print(f"  ❌ TikTok respondió {put_response.status_code} al subir el trozo {i+1}/{total_chunk_count}: {put_response.text}")
            put_response.raise_for_status()


def _load_tokens() -> dict:
    if not os.path.exists(TOKENS_FILE):
        raise FileNotFoundError(
            "❌ No hay tokens guardados. Ejecuta primero: python tiktok_auth.py"
        )
    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)


def _refresh_access_token(tokens: dict) -> dict:
    response = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": CLIENT_KEY,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
    )
    response.raise_for_status()
    new_tokens = response.json()
    _save_tokens(new_tokens)
    return new_tokens


def _init_upload(access_token: str, video_size: int, chunk_size: int, total_chunk_count: int):
    return requests.post(
        "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunk_count,
            }
        },
    )


def upload_video_as_draft(video_path: str) -> str:
    """
    Sube un vídeo a la bandeja de borradores de la cuenta autorizada.
    Renueva el access_token automáticamente si ha caducado.
    Devuelve el publish_id que da TikTok.
    """
    print(f"\n📤 Subiendo a TikTok como borrador: {video_path}")

    tokens = _load_tokens()
    access_token = tokens["access_token"]
    video_size = os.path.getsize(video_path)
    chunk_size, total_chunk_count = _compute_chunk_plan(video_size)
    print(f"  📦 {video_size / (1024*1024):.1f}MB en {total_chunk_count} trozo(s)")

    init_response = _init_upload(access_token, video_size, chunk_size, total_chunk_count)

    if init_response.status_code == 401:
        print("  🔄 Token caducado, renovando...")
        tokens = _refresh_access_token(tokens)
        access_token = tokens["access_token"]
        init_response = _init_upload(access_token, video_size, chunk_size, total_chunk_count)

    if not init_response.ok:
        print(f"  ❌ TikTok respondió {init_response.status_code}: {init_response.text}")
    init_response.raise_for_status()
    init_data = init_response.json()

    if "data" not in init_data or "upload_url" not in init_data["data"]:
        raise RuntimeError(f"❌ Error iniciando subida: {init_data}")

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    _put_video_chunks(upload_url, video_path, video_size, chunk_size, total_chunk_count)

    print(f"✅ Subido como borrador (publish_id: {publish_id})")
    print("   📱 Abre TikTok en el móvil para revisarlo y publicarlo.")
    return publish_id


def _get_creator_info(access_token: str):
    return requests.post(
        "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
    )


def _init_direct_post(access_token: str, video_size: int, chunk_size: int, total_chunk_count: int, title: str, privacy_level: str):
    return requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={
            "post_info": {
                "title": title,
                "privacy_level": privacy_level,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunk_count,
            },
        },
    )


def upload_video_direct_post(video_path: str, title: str, privacy_level: str = "SELF_ONLY") -> str:
    """
    Publica el vídeo directamente en el perfil de la cuenta autorizada, con
    el título/hashtags ya puestos (el parámetro 'title' puede incluir
    #hashtags, TikTok los detecta solo). Mientras la app no esté auditada,
    TikTok fuerza que la publicación quede como privada (SELF_ONLY) — hay
    que entrar a la app y cambiarla a pública a mano.
    Devuelve el publish_id que da TikTok.
    """
    print(f"\n📤 Publicando en TikTok (privado, con título/hashtags): {video_path}")

    tokens = _load_tokens()
    access_token = tokens["access_token"]

    info_response = _get_creator_info(access_token)
    if info_response.status_code == 401:
        tokens = _refresh_access_token(tokens)
        access_token = tokens["access_token"]
        info_response = _get_creator_info(access_token)
    info_response.raise_for_status()
    info_data = info_response.json()

    available = info_data.get("data", {}).get("privacy_level_options", [])
    if available and privacy_level not in available:
        print(f"  ⚠️ '{privacy_level}' no disponible para esta cuenta, usando '{available[0]}' en su lugar.")
        privacy_level = available[0]

    video_size = os.path.getsize(video_path)
    chunk_size, total_chunk_count = _compute_chunk_plan(video_size)
    print(f"  📦 {video_size / (1024*1024):.1f}MB en {total_chunk_count} trozo(s)")

    init_response = _init_direct_post(access_token, video_size, chunk_size, total_chunk_count, title, privacy_level)

    if init_response.status_code == 401:
        print("  🔄 Token caducado, renovando...")
        tokens = _refresh_access_token(tokens)
        access_token = tokens["access_token"]
        init_response = _init_direct_post(access_token, video_size, chunk_size, total_chunk_count, title, privacy_level)

    if not init_response.ok:
        print(f"  ❌ TikTok respondió {init_response.status_code}: {init_response.text}")
    init_response.raise_for_status()
    init_data = init_response.json()

    if "data" not in init_data or "upload_url" not in init_data["data"]:
        raise RuntimeError(f"❌ Error iniciando subida: {init_data}")

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    _put_video_chunks(upload_url, video_path, video_size, chunk_size, total_chunk_count)

    print(f"✅ Publicado en modo {privacy_level} (publish_id: {publish_id})")
    print("   📱 Abre TikTok en el móvil para cambiarlo a público.")
    return publish_id


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python tiktok_upload.py <ruta_del_video.mp4>")
    else:
        upload_video_as_draft(sys.argv[1])
