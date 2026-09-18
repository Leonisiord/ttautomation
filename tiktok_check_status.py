"""
TikTok — Comprobar el estado de una subida
=============================================
Usa el publish_id que te dio el script principal al subir el vídeo,
y te dice si TikTok lo está procesando, si ya está en tu bandeja
(SEND_TO_USER_INBOX) o si falló y por qué (FAILED + fail_reason).

Uso: python tiktok_check_status.py <publish_id>
"""

import sys
import json
import requests
from tiktok_upload import _load_tokens, _refresh_access_token


def check_status(publish_id: str):
    tokens = _load_tokens()
    access_token = tokens["access_token"]

    def _fetch():
        return requests.post(
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id},
        )

    response = _fetch()
    if response.status_code == 401:
        tokens = _refresh_access_token(tokens)
        access_token = tokens["access_token"]
        response = _fetch()

    response.raise_for_status()
    data = response.json()
    print(json.dumps(data, indent=2, ensure_ascii=False))

    status = data.get("data", {}).get("status")
    if status == "FAILED":
        print(f"\n❌ Falló. Motivo: {data['data'].get('fail_reason')}")
    elif status == "SEND_TO_USER_INBOX":
        print("\n✅ Está en tu bandeja de TikTok — ábrelo en el móvil.")
    elif status in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"):
        print("\n⏳ Todavía procesando, espera un poco y vuelve a comprobar.")
    else:
        print(f"\nℹ️ Estado: {status}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python tiktok_check_status.py <publish_id>")
    else:
        check_status(sys.argv[1])
