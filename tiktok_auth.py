"""
TikTok — Autorización OAuth (se ejecuta UNA SOLA VEZ)
=======================================================
Abre el navegador para iniciar sesión con la cuenta DEL CANAL y
autorizar la app. Guarda el access_token y refresh_token en
tiktok_tokens.json para que el resto de scripts los reutilicen.

Ejecuta: python tiktok_auth.py
"""

import os
import json
import base64
import hashlib
import secrets
import webbrowser
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, quote
from dotenv import load_dotenv

load_dotenv()
CLIENT_KEY    = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
# TikTok exige HTTPS incluso para pruebas — usa la URL https que te da ngrok,
# terminada en /callback. Ponla en tu .env como TIKTOK_REDIRECT_URI.
REDIRECT_URI  = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES        = "video.upload,video.publish,user.info.basic"

TOKENS_FILE = "tiktok_tokens.json"

state = secrets.token_urlsafe(16)
received = {}


def _make_pkce_pair():
    """PKCE: genera un code_verifier secreto y su code_challenge (SHA256, base64url)."""
    verifier = secrets.token_urlsafe(64)[:64]
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return verifier, challenge


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            received["code"] = params["code"][0]
            received["state"] = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<h2>Listo, ya puedes cerrar esta pestaña y volver a la terminal.</h2>".encode("utf-8")
            )
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # silenciar logs del mini-servidor


def main():
    if not CLIENT_KEY or not CLIENT_SECRET:
        raise ValueError("❌ Faltan TIKTOK_CLIENT_KEY o TIKTOK_CLIENT_SECRET en .env")
    if not REDIRECT_URI:
        raise ValueError(
            "❌ Falta TIKTOK_REDIRECT_URI en .env — pon ahí la URL https que te da ngrok "
            "(termina en /callback), por ejemplo: https://abc123.ngrok-free.app/callback"
        )

    code_verifier, code_challenge = _make_pkce_pair()

    auth_url = (
        "https://www.tiktok.com/v2/auth/authorize/"
        f"?client_key={CLIENT_KEY}"
        f"&scope={SCOPES}"
        f"&response_type=code"
        f"&redirect_uri={quote(REDIRECT_URI, safe='')}"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )

    print(f"👉 REDIRECT_URI usado: {REDIRECT_URI}")
    print(f"👉 URL completa de autorización:\n{auth_url}\n")
    print("🔑 Abriendo el navegador para autorizar la app...")
    print("   ⚠️  Inicia sesión con la cuenta DEL CANAL, no tu cuenta personal.")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    print("⏳ Esperando autorización en el navegador...")
    while "code" not in received:
        server.handle_request()

    if received.get("state") != state:
        raise ValueError("❌ El parámetro 'state' no coincide — aborta por seguridad.")

    code = received["code"]
    print("✅ Código recibido, canjeando por tokens de acceso...")

    response = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": CLIENT_KEY,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        },
    )
    response.raise_for_status()
    tokens = response.json()

    if "access_token" not in tokens:
        print("❌ Error al obtener tokens:", tokens)
        return

    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)

    print(f"✅ Tokens guardados en {TOKENS_FILE}")
    print("   A partir de ahora, la subida automática los usa (y se renuevan solos).")


if __name__ == "__main__":
    main()
