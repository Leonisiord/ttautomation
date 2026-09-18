"""
TikTok/Shorts — Reddit Story Pipeline
======================================
Stack gratuito:
  - Google Gemini   → historia dramática en frases cortas + género del narrador
  - Edge TTS        → narración continua con voz natural (gratis, sin API key)
  - FFmpeg          → vídeo vertical con texto sincronizado

⚠️  Requisito: pon tu vídeo de Minecraft como 'background.mp4'
    en esta misma carpeta antes de ejecutar.
"""

import os
import re
import json
import uuid
import random
import asyncio
import subprocess
from pathlib import Path
import edge_tts
from dotenv import load_dotenv
from google import genai
from tiktok_upload import upload_video_as_draft, upload_video_direct_post

# Pon esto en False si alguna vez quieres generar el vídeo sin subirlo
AUTO_UPLOAD_TIKTOK = True

# "direct_post" → publica ya con título/hashtags puestos, pero en privado.
#                 ⚠️ TikTok NO permite este modo mientras la app esté en
#                 Sandbox (da 403) — solo funcionará una vez pase la
#                 auditoría completa y la app esté en modo Live/Producción.
# "draft"       → sube a la bandeja de borradores sin título/hashtags (hay
#                 que ponerlos a mano al abrir el borrador en el móvil).
#                 Es el único modo que Sandbox permite — el que ya usábamos.
TIKTOK_UPLOAD_MODE = "draft"

# ── Configuración ──────────────────────────────────────────────────────────────
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("❌ No se encontró GEMINI_API_KEY en el archivo .env")

gemini = genai.Client(api_key=GEMINI_API_KEY)

BACKGROUND = Path("background.mp4")
FONT_NAME  = "Arial"

# Pool de voces según el género del narrador (primera persona de la historia).
# Se elige primero el género, y luego una voz al azar dentro de esa lista —
# así no siempre suena la misma persona.
VOICES = {
    "male": [
        "es-ES-AlvaroNeural",
        "es-MX-JorgeNeural",
        "es-AR-TomasNeural",
    ],
    "female": [
        "es-ES-ElviraNeural",
        "es-MX-DaliaNeural",
        "es-AR-ElenaNeural",
    ],
}

# Velocidad de la narración. "+0%" = normal. Sube esto si sigue sonando lenta.
SPEECH_RATE = "+8%"

# Nº de frases por llamada a Edge TTS. Sintetizar la historia ENTERA de una
# vez (40+ frases) hace que el motor la trocee internamente, y en ese corte
# se oye un pequeño "trabarse" de 1-2s. Partiéndolo en tandas más pequeñas y
# pegando el audio sin silencio extra, evita el problema y sigue sonando
# continuo.
TTS_CHUNK_SIZE = 6

# Cada short arranca en un punto aleatorio del vídeo de fondo (entre el
# minuto 0 y este límite), para no repetir siempre las mismas imágenes.
BACKGROUND_MAX_START_MIN = 28

OUTPUT_DIR = Path("output")
SHORTS_DIR = OUTPUT_DIR / "shorts"

for folder in [SHORTS_DIR]:
    folder.mkdir(parents=True, exist_ok=True)


def _clean_json(text: str) -> str:
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


# ── PASO 0: Lluvia de premisas y elección aleatoria ───────────────────────────
def generate_topics(n: int = 10) -> list:
    print("💡 Generando lista de premisas...")

    prompt = f"""
Eres un experto en contenido viral de TikTok e Instagram Reels, especializado en
historias dramáticas estilo Reddit (AITA, bodas, familia, herencias, traiciones,
venganzas, vecinos, amistades, trabajo). Conoces bien qué tipo de premisas
funcionan mejor ahora mismo en ese nicho.

Genera {n} premisas de historia MUY DIFERENTES entre sí — distintos escenarios,
relaciones y conflictos. Cada una es solo el conflicto central sin resolverlo,
una frase.

Devuelve SOLO JSON, sin markdown:
{{"topics": ["premisa 1", "premisa 2", "..."]}}
"""

    response = gemini.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
        config={"temperature": 1.3},
    )
    data = json.loads(_clean_json(response.text))
    topics = data["topics"]
    print(f"✅ {len(topics)} premisas generadas")
    return topics


# ── PASO 1: Generar historia con Gemini ───────────────────────────────────────
def generate_story(topic: str) -> dict:
    print(f"📝 Generando historia sobre: {topic}")

    prompt = f"""
Eres un creador de contenido viral para TikTok y YouTube Shorts en español.
Crea una historia dramática corta estilo Reddit, narrada en primera persona ("yo"),
que gire en torno a esta premisa concreta:

"{topic}"

La historia debe ser muy adictiva, con un giro inesperado al final. Desarrolla
libremente los detalles, nombres y personajes — la premisa es solo el punto de
partida.

Devuelve SOLO JSON válido, sin markdown ni explicaciones:

{{
  "title": "Título corto y llamativo (max 60 caracteres)",
  "description": "Descripción para YouTube con emojis (max 150 caracteres)",
  "tags": ["shorts", "historia", "drama", "reddit", "viral"],
  "narrator_gender": "male o female — el género de quien narra en primera persona",
  "phrases": [
    "Frase corta de máximo 6 palabras.",
    "Otra frase igual de corta.",
    "..."
  ]
}}

Reglas:
- Entre 35 y 45 frases en total
- Cada frase: máximo 6 palabras, impactante y clara
- La primera frase debe enganchar al instante
- Desarrolla la historia con más contexto y detalles antes del giro
- Incluir giro dramático hacia el final
- Terminar con una pregunta al espectador
- "narrator_gender" debe ser exactamente "male" o "female"
- Devolver SOLO el JSON
"""

    response = gemini.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
        config={"temperature": 1.1},
    )
    content = json.loads(_clean_json(response.text))

    gender = content.get("narrator_gender", "female").strip().lower()
    if gender not in VOICES:
        gender = "female"
    content["narrator_gender"] = gender
    content["topic"] = topic

    print(f"✅ Historia: {content['title']}")
    print(f"   {len(content['phrases'])} frases generadas — narrador: {gender}")
    return content


# ── PASO 2: Generar audio continuo con Edge TTS (voz natural) ────────────────
async def _synthesize_full(text: str, voice: str, filepath: Path) -> list:
    """
    Sintetiza TODO el guión de una vez (sin cortes entre frases) y devuelve
    los eventos WordBoundary que da Edge TTS, usados luego para calcular
    en qué momento exacto se dice cada frase.
    """
    communicate = edge_tts.Communicate(text, voice=voice, rate=SPEECH_RATE)
    word_events = []

    with open(filepath, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_events.append(chunk)

    return word_events


def _prep_for_tts(phrase: str) -> str:
    """Quita el punto final (produce pausa larga) mantiene ¿?/¡! (pausa corta deseada)."""
    p = phrase.strip()
    if p.endswith(".") and not p.endswith("..."):
        p = p[:-1]
    return p


def _align_events_to_phrases(tts_phrases: list, word_events: list, real_duration: float) -> list:
    """
    Reparte los eventos de palabra de Edge TTS entre las frases, en orden,
    contando palabras (simple y robusto). Si en algún punto nos quedamos sin
    eventos —los motores TTS no siempre emiten uno por cada palabra—, el
    tiempo que falta se reparte proporcionalmente entre las frases restantes
    según su longitud, para que la historia SIEMPRE llegue hasta el final
    real del audio en vez de apelotonarse al principio.
    """
    n = len(tts_phrases)
    timestamps = [None] * n
    idx = 0

    for i, tts_phrase in enumerate(tts_phrases):
        n_words = max(len(re.findall(r"\S+", tts_phrase)), 1)
        chunk = word_events[idx: idx + n_words]

        t_start = timestamps[i - 1][1] if i > 0 else 0.0

        if chunk:
            t_end = chunk[-1]["end_s"]
            idx += n_words
        else:
            remaining = tts_phrases[i:]
            remaining_chars = sum(len(p) for p in remaining) or 1
            share = len(tts_phrase) / remaining_chars
            t_end = t_start + (real_duration - t_start) * share

        timestamps[i] = (t_start, max(t_end, t_start + 0.3))

    return timestamps


def _chunk_list(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _synthesize_chunk_timestamps(chunk_phrases: list, chunk_path: Path, voice: str) -> tuple:
    """Sintetiza un trozo pequeño de frases y devuelve (duración_real, timestamps_locales)."""
    text = ", ".join(chunk_phrases)
    word_events = asyncio.run(_synthesize_full(text, voice, chunk_path))

    real_duration = get_duration(str(chunk_path))
    raw_end = (word_events[-1]["offset"] + word_events[-1]["duration"]) if word_events else 1
    scale = real_duration / raw_end if raw_end else 1.0

    for ev in word_events:
        ev["start_s"] = ev["offset"] * scale
        ev["end_s"]   = (ev["offset"] + ev["duration"]) * scale

    local_timestamps = _align_events_to_phrases(chunk_phrases, word_events, real_duration)
    return real_duration, local_timestamps


def _concat_audio_seamless(chunk_paths: list, output_path: Path):
    """
    Pega varios mp3 en uno solo, a nivel de muestra (sin silencio extra ni
    clics de por medio) usando el filtro 'concat' de FFmpeg en vez del
    demuxer -c copy, que puede dejar micro-cortes entre segmentos.
    """
    if len(chunk_paths) == 1:
        subprocess.run(
            ["ffmpeg", "-y", "-i", unix_path(chunk_paths[0]), "-c:a", "libmp3lame",
             unix_path(output_path)],
            check=True, capture_output=True,
        )
        return

    cmd = ["ffmpeg", "-y"]
    for p in chunk_paths:
        cmd += ["-i", unix_path(p)]

    filter_inputs = "".join(f"[{i}:a]" for i in range(len(chunk_paths)))
    filter_complex = f"{filter_inputs}concat=n={len(chunk_paths)}:v=0:a=1[out]"

    cmd += ["-filter_complex", filter_complex, "-map", "[out]",
            "-c:a", "libmp3lame", unix_path(output_path)]

    subprocess.run(cmd, check=True, capture_output=True)


def generate_audio(content: dict, run_id: str) -> tuple:
    voice = random.choice(VOICES[content["narrator_gender"]])
    print(f"\n🎙️ Generando narración con Edge TTS ({voice}, {SPEECH_RATE})...")

    phrases     = content["phrases"]
    tts_phrases = [_prep_for_tts(p) for p in phrases]
    chunks      = _chunk_list(tts_phrases, TTS_CHUNK_SIZE)
    audio_path  = OUTPUT_DIR / f"combined_audio_{run_id}.mp3"

    chunk_paths  = []
    timestamps   = []
    running_offset = 0.0

    print(f"  🎬 Sintetizando en {len(chunks)} tandas de hasta {TTS_CHUNK_SIZE} frases...")
    for ci, chunk_phrases in enumerate(chunks):
        chunk_path = OUTPUT_DIR / f"_tts_chunk_{run_id}_{ci}.mp3"
        real_duration, local_timestamps = _synthesize_chunk_timestamps(chunk_phrases, chunk_path, voice)

        for (start, end) in local_timestamps:
            timestamps.append((start + running_offset, end + running_offset))

        running_offset += real_duration
        chunk_paths.append(chunk_path)

    _concat_audio_seamless(chunk_paths, audio_path)

    for p in chunk_paths:
        p.unlink(missing_ok=True)

    print(f"✅ Narración generada: {audio_path}")
    return str(audio_path), timestamps


# ── PASO 3: Montar vídeo con FFmpeg ───────────────────────────────────────────
def get_duration(filepath: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", filepath],
        capture_output=True, text=True
    )
    try:
        return float(result.stdout.strip())
    except:
        return 1.5


def unix_path(p) -> str:
    return str(p).replace("\\", "/")


def pick_background_start(total_duration: float) -> float:
    """
    Elige un punto de arranque aleatorio dentro de background.mp4 (entre 0
    y BACKGROUND_MAX_START_MIN), asegurando que quede metraje suficiente
    por delante para cubrir la duración del short sin tener que dar la
    vuelta al vídeo.
    """
    bg_duration = get_duration(str(BACKGROUND))
    max_start = min(BACKGROUND_MAX_START_MIN * 60, bg_duration - total_duration - 5)
    max_start = max(max_start, 0)
    start = random.uniform(0, max_start) if max_start > 0 else 0.0
    print(f"  🎲 Fondo: arrancando en el minuto {start/60:.1f}")
    return start


def format_ass_time(seconds: float) -> str:
    """Formatea segundos como H:MM:SS.CC (centésimas) para .ass"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_subtitles(phrases: list, timestamps: list, srt_path: Path):
    """
    Genera un archivo .ass con estilo grande, blanco con borde negro,
    centrado — como los subtítulos de TikTok. Un archivo .ass escala
    perfectamente aunque haya 100 frases; encadenar drawtext no.
    """
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},78,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,0,5,60,60,100,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for phrase, (start, end) in zip(phrases, timestamps):
        text = phrase.replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},"
            f"Default,,0,0,0,,{text}\n"
        )

    with open(srt_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def assemble_video(content: dict, audio_path: str, timestamps: list, run_id: str) -> str:
    print("\n✂️  Montando vídeo con FFmpeg...")

    total_duration = get_duration(audio_path)
    bg_start = pick_background_start(total_duration)

    # Generar archivo de subtítulos .ass sincronizado con el audio
    phrases = content["phrases"]
    subs_path = OUTPUT_DIR / f"story_{run_id}.ass"
    build_subtitles(phrases, timestamps, subs_path)

    # FFmpeg necesita la ruta del filtro subtitles con los dos puntos
    # de la unidad escapados (C:/...) y entre comillas simples
    subs_filter_path = unix_path(subs_path.resolve()).replace(":", "\\:")

    # Cadena de filtros: escalar + recortar a vertical + subtítulos
    vf_chain = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"subtitles='{subs_filter_path}'"
    )

    safe_title = "".join(
        c if c.isalnum() or c in " _-" else "" for c in content["title"]
    ).strip()
    output_path = SHORTS_DIR / f"{safe_title} [{run_id}].mp4"

    print(f"  Ensamblando vídeo vertical 1080x1920...", end=" ", flush=True)
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", f"{bg_start:.2f}",       # Arranca en un punto aleatorio del fondo
        "-stream_loop", "-1",          # Repite el background hasta cubrir la duración
        "-i", unix_path(BACKGROUND),
        "-i", unix_path(audio_path),
        "-map", "0:v:0",                # Vídeo: del background
        "-map", "1:a:0",                # Audio: SOLO la narración TTS
        "-vf", vf_chain,
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-t", str(total_duration),     # Duración exacta de la narración
        "-shortest",
        unix_path(output_path)
    ], check=True, capture_output=True)
    print("✓")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"✅ Short guardado: {output_path} ({size_mb:.1f} MB)")
    return str(output_path)


# ── PASO 4: Guardar metadatos ─────────────────────────────────────────────────
def save_metadata(content: dict, video_path: str, run_id: str) -> dict:
    metadata = {
        "title":       content["title"],
        "description": content["description"],
        "tags":        content["tags"],
        "video_path":  video_path,
    }
    meta_file = SHORTS_DIR / f"metadata_{run_id}.json"
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    print(f"\n📋 Metadatos: {meta_file}")
    return metadata


def build_tiktok_caption(content: dict) -> str:
    """
    Arma el texto que TikTok usará como título/descripción del post,
    combinando la descripción de la historia con sus hashtags. TikTok
    detecta los "#hashtag" automáticamente dentro del texto.
    """
    hashtags = " ".join(
        f"#{tag.strip().replace(' ', '')}" for tag in content.get("tags", []) if tag.strip()
    )
    caption = f"{content['description']} {hashtags}".strip()
    return caption[:2200]  # límite de TikTok (caracteres UTF-16)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    if not BACKGROUND.exists():
        raise FileNotFoundError(
            "❌ No se encontró background.mp4\n"
            "   Descarga el vídeo de Minecraft y ponlo en esta carpeta con ese nombre."
        )

    print("🚀 TikTok/Shorts — Reddit Story Pipeline")
    print("=" * 50)

    run_id = uuid.uuid4().hex[:8]

    topics                  = generate_topics()
    topic                   = random.choice(topics)
    content                 = generate_story(topic)
    audio_path, timestamps  = generate_audio(content, run_id)
    video_path              = assemble_video(content, audio_path, timestamps, run_id)
    metadata                = save_metadata(content, video_path, run_id)

    if AUTO_UPLOAD_TIKTOK:
        try:
            if TIKTOK_UPLOAD_MODE == "direct_post":
                caption = build_tiktok_caption(content)
                upload_video_direct_post(video_path, caption)
            else:
                upload_video_as_draft(video_path)
        except Exception as e:
            print(f"⚠️ No se pudo subir a TikTok automáticamente: {e}")
            print("   El vídeo sigue en tu carpeta, puedes subirlo a mano.")

    print("\n" + "=" * 50)
    print("🎉 ¡Short completado!")
    print(f"📱  Vídeo:  {video_path}")
    print(f"📋  Título: {metadata['title']}")
    print("\n➡️  Revisa el borrador en TikTok y publícalo desde el móvil")


if __name__ == "__main__":
    main()
