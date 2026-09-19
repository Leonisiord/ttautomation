"""
review_publish.py — Revisar y publicar en TikTok, con un clic por vídeo
==========================================================================
Esto se ejecuta EN TU ORDENADOR (no en GitHub Actions). Es la pieza que
hace que el pipeline cumpla lo que exige la auditoría de TikTok: que una
persona vea una vista previa real del vídeo, pueda editar el título y las
opciones de privacidad/interacciones, y pulse "Publicar" a propósito —
nada se publica sin ese clic.

Flujo:
1. Al arrancar, descarga (con `gh`) los vídeos + metadatos pendientes de la
   GitHub Release "pending-review" (los que main_reddit.py fue dejando ahí
   cuando TIKTOK_UPLOAD_MODE = "review") a una carpeta local.
2. Abre http://localhost:5000 — verás la lista de vídeos pendientes.
3. Entras en uno, ves la vista previa reproducible, editas el título si
   quieres, eliges privacidad (las opciones vienen de la propia API de
   TikTok, no están puestas a mano) y comentarios/dúo/stitch.
4. Pulsas "Publicar". Se sube por el endpoint de Direct Post y, si todo
   va bien, se borra de la cola de revisión (tanto local como en la
   Release) para que no te vuelva a aparecer.

Requisitos: pip install flask requests python-dotenv, y tener `gh`
instalado y autenticado (gh auth login) con permiso sobre el repo.
"""

import os
import json
import shutil
import subprocess
from pathlib import Path

from flask import Flask, render_template_string, request, redirect, url_for, send_file, flash

from tiktok_upload import upload_video_direct_post, get_creator_info
from tiktok_check_status import check_status

REPO = os.getenv("GITHUB_REPO")  # ej: "Leonisiord/ttautomation" — o dilo a mano abajo
PENDING_DIR = Path("pending_review")
PENDING_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = "solo-para-flash-messages-localhost"


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def sync_from_release():
    """Trae de la Release 'pending-review' lo que aún no tengamos en local."""
    repo_args = ["--repo", REPO] if REPO else []
    result = _run(["gh", "release", "download", "pending-review", *repo_args,
                    "--pattern", "*", "--dir", str(PENDING_DIR), "--clobber", "--skip-existing"])
    if result.returncode != 0 and "release not found" not in result.stderr.lower():
        print("⚠️ No se pudo sincronizar con la Release 'pending-review':", result.stderr.strip())


def _remove_from_release(filenames: list):
    repo_args = ["--repo", REPO] if REPO else []
    for name in filenames:
        _run(["gh", "release", "delete-asset", "pending-review", name, "-y", *repo_args])


def list_pending() -> list:
    items = []
    for review_file in sorted(PENDING_DIR.glob("review_*.json")):
        with open(review_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        video_path = PENDING_DIR / Path(data["video_path"]).name
        if not video_path.exists():
            continue  # el vídeo aún no ha terminado de bajar, o ya se publicó
        data["_review_file"] = review_file.name
        data["_video_file"] = video_path.name
        items.append(data)
    return items


LIST_TEMPLATE = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Vídeos pendientes de revisar</title>
<style>
body{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;background:#111;color:#eee}
a{color:#4da3ff} .card{background:#1c1c1c;border-radius:12px;padding:16px;margin-bottom:14px}
.flash{background:#2d4a2d;padding:10px 14px;border-radius:8px;margin-bottom:16px}
</style></head><body>
<h2>📥 Pendientes de revisar ({{ items|length }})</h2>
{% with messages = get_flashed_messages() %}
  {% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}
{% endwith %}
{% for item in items %}
<div class="card">
  <strong>{{ item.title }}</strong><br>
  <small>{{ item._video_file }}</small><br>
  <a href="{{ url_for('review', review_file=item._review_file) }}">Revisar y publicar →</a>
</div>
{% else %}
<p>No hay nada pendiente ahora mismo. Ejecuta el pipeline o pulsa recargar.</p>
{% endfor %}
<p><a href="{{ url_for('sync') }}">🔄 Volver a sincronizar con GitHub</a></p>
</body></html>
"""

REVIEW_TEMPLATE = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Revisar antes de publicar</title>
<style>
body{font-family:system-ui,sans-serif;max-width:420px;margin:20px auto;padding:0 16px;background:#111;color:#eee}
video{width:100%;border-radius:12px;background:#000}
textarea{width:100%;min-height:90px;font-family:inherit;font-size:15px;border-radius:8px;padding:8px}
select,button{font-size:16px;padding:10px;border-radius:8px;width:100%;margin-top:8px}
label{display:block;margin-top:10px}
.row{display:flex;gap:8px;align-items:center}
button{background:#fe2c55;color:#fff;border:none;font-weight:bold;margin-top:20px}
a{color:#4da3ff}
</style></head><body>
<p><a href="{{ url_for('index') }}">← Volver a la lista</a></p>
<video controls src="{{ url_for('video_file', name=item._video_file) }}"></video>

<form method="post">
  <label>Título / caption (con hashtags)</label>
  <textarea name="caption" maxlength="2200">{{ item.caption }}</textarea>

  <label>Privacidad</label>
  <select name="privacy_level">
    {% for opt in privacy_options %}
      <option value="{{ opt }}">{{ opt }}</option>
    {% endfor %}
  </select>

  <div class="row"><input type="checkbox" name="disable_comment" id="dc" {{ 'disabled checked' if comment_disabled else '' }}>
    <label for="dc" style="margin:0">Desactivar comentarios</label></div>
  <div class="row"><input type="checkbox" name="disable_duet" id="dd" {{ 'disabled checked' if duet_disabled else '' }}>
    <label for="dd" style="margin:0">Desactivar dúo</label></div>
  <div class="row"><input type="checkbox" name="disable_stitch" id="ds" {{ 'disabled checked' if stitch_disabled else '' }}>
    <label for="ds" style="margin:0">Desactivar stitch</label></div>

  <div class="row"><input type="checkbox" name="agree" id="ag" required>
    <label for="ag" style="margin:0">He revisado el vídeo y confirmo su publicación en @{{ creator_nickname }}</label></div>

  <button type="submit">📤 Publicar en TikTok</button>
</form>
</body></html>
"""


@app.route("/")
def index():
    return render_template_string(LIST_TEMPLATE, items=list_pending())


@app.route("/sync")
def sync():
    sync_from_release()
    flash("Sincronizado con GitHub.")
    return redirect(url_for("index"))


@app.route("/video/<name>")
def video_file(name):
    return send_file(PENDING_DIR / name)


@app.route("/review/<review_file>", methods=["GET", "POST"])
def review(review_file):
    review_path = PENDING_DIR / review_file
    with open(review_path, "r", encoding="utf-8") as f:
        item = json.load(f)
    item["_review_file"] = review_file
    item["_video_file"] = Path(item["video_path"]).name

    info = get_creator_info()

    if request.method == "POST":
        video_path = PENDING_DIR / item["_video_file"]
        publish_id = upload_video_direct_post(
            str(video_path),
            title=request.form["caption"],
            privacy_level=request.form["privacy_level"],
            disable_comment="disable_comment" in request.form,
            disable_duet="disable_duet" in request.form,
            disable_stitch="disable_stitch" in request.form,
        )
        # Limpieza: ya publicado, fuera de la cola local y de la Release.
        video_path.unlink(missing_ok=True)
        review_path.unlink(missing_ok=True)
        _remove_from_release([item["_video_file"], review_file])

        flash(f"✅ Enviado a publicar (publish_id: {publish_id}). Comprobando estado...")
        check_status(publish_id)  # imprime el estado en la terminal donde corre Flask
        return redirect(url_for("index"))

    return render_template_string(
        REVIEW_TEMPLATE,
        item=item,
        privacy_options=info["privacy_level_options"] or ["SELF_ONLY"],
        comment_disabled=info["comment_disabled"],
        duet_disabled=info["duet_disabled"],
        stitch_disabled=info["stitch_disabled"],
        creator_nickname=info["creator_nickname"],
    )


if __name__ == "__main__":
    sync_from_release()
    print("\n👉 Abre http://localhost:5000 en el navegador\n")
    app.run(port=5000, debug=False)
