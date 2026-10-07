"""App de vocabulaire : Tally -> Groq (+ voix ElevenLabs, + image IA) -> base -> Anki / flashcards.

Plusieurs paquets (DECKS) dans la même app : français → anglais, allemand → anglais,
anglais ↔ polonais. Le paquet français garde exactement les mêmes identifiants qu'avant
(cartes, audios, paquet Anki) : rien n'est dupliqué dans Anki.

Bibliothèque standard de Python, plus `genanki` pour l'export .apkg (python -m pip install genanki).

Lancer :  python3 app.py
Pages (chacune accepte ?deck=fr-en, de-en ou en-pl ; fr-en par défaut) :
  GET  /             page d'accueil (liste des mots + ajout manuel)
  GET  /cartes       flashcards façon Quizlet (glisser à droite = je sais, à gauche = à revoir)
  POST /add          ajout manuel d'un mot
  POST /webhook/tally   reçoit les soumissions Tally (?deck=…&lang=… pour un autre paquet)
  GET  /export.apkg  paquet Anki avec l'audio et les images (recommandé)
  GET  /export.csv   CSV pour Anki (l'audio et les images sont à copier à part : /audio.zip)
"""

import base64
import csv
import hashlib
import hmac
import html
import io
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def load_env_file():
    """Lit le fichier .env à côté de app.py (lignes CLE=valeur), sans écraser l'environnement."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:  # -sig : tolère le BOM ajouté par le Bloc-notes
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").removeprefix("$env:").strip()
            value = value.strip().strip('"').strip("'")
            if key and value and not os.environ.get(key):
                os.environ[key] = value


load_env_file()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
TALLY_SIGNING_SECRET = os.environ.get("TALLY_SIGNING_SECRET", "")
# Mot de passe des pages web (indispensable une fois l'app en ligne). Vide = pas de mot de passe.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# Base Postgres en ligne (Supabase). Vide = fichier SQLite local (DB_PATH).
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "words.db"))
PORT = int(os.environ.get("PORT", "8000"))
# Voix ElevenLabs (optionnel : sans clé, les cartes n'ont simplement pas d'audio).
# ELEVENLABS_VOICE_ID_DE, _EN, _PL… permettent une autre voix pour une langue donnée.
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
AUDIO_DIR = os.environ.get("AUDIO_DIR", os.path.join(os.path.dirname(DB_PATH), "audio"))
# Images IA : "pollinations" (gratuit, sans clé), "openai" (OPENAI_API_KEY) ou "aucun".
IMAGE_PROVIDER = os.environ.get("IMAGE_PROVIDER", "pollinations").strip().lower()
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
POLLINATIONS_API_KEY = os.environ.get("POLLINATIONS_API_KEY", "")
# IMAGES_AUTO=1 : une image pour chaque nouveau mot. Sinon, bouton 🖼 sur la page.
IMAGES_AUTO = os.environ.get("IMAGES_AUTO", "").strip().lower() in ("1", "true", "oui", "yes")

# Langues : (nom en français pour Groq, nom affiché dans l'app)
LANGS = {
    "fr": ("français", "Français"),
    "de": ("allemand", "Deutsch"),
    "en": ("anglais", "English"),
    "pl": ("polonais", "Polski"),
}
# Noms acceptés dans un champ « Langue » de Tally ou dans ?lang=
LANG_ALIASES = {
    "fr": "fr", "français": "fr", "francais": "fr", "french": "fr",
    "de": "de", "allemand": "de", "deutsch": "de", "german": "de",
    "en": "en", "anglais": "en", "english": "en", "angielski": "en",
    "pl": "pl", "polonais": "pl", "polski": "pl", "polish": "pl",
}

# Paquets. langs = (langue apprise, langue de traduction). input = langue des mots écrits
# par défaut ("auto" : Groq devine si le mot est dans l'une ou l'autre).
# Le paquet fr-en garde les identifiants d'avant (anki_deck, model, prefix) : ne pas les changer.
DECKS = {
    "fr-en": {"langs": ("fr", "en"), "input": "fr", "title": "Français → English",
              "anki_name": "Français", "anki_deck": 2059400110, "model": 1607392319,
              "prefix": "motsfr", "tag": "francais"},
    "de-en": {"langs": ("de", "en"), "input": "de", "title": "Deutsch → English",
              "anki_name": "Deutsch", "anki_deck": 2059400111, "model": 1607392320,
              "prefix": "motsde", "tag": "deutsch"},
    "en-pl": {"langs": ("en", "pl"), "input": "auto", "title": "English ↔ Polski",
              "anki_name": "English ↔ Polski", "anki_deck": 2059400112, "model": 1607392320,
              "prefix": "motsenpl", "tag": "english_polski"},
}
DEFAULT_DECK = "fr-en"

# Libellés des champs du formulaire Tally (insensible à la casse).
WORD_LABELS = {"mot", "mot en français", "word", "french word", "wort", "słowo", "slowo"}
CONTEXT_LABELS = {"contexte", "context", "note", "kontext", "kontekst"}
LANG_LABELS = {"langue", "language", "sprache", "język", "jezyk"}


def deck_of(value):
    return value if value in DECKS else DEFAULT_DECK


def lang_of(value, deck):
    """Code de langue valide pour ce paquet, sinon la langue d'entrée par défaut du paquet."""
    code = LANG_ALIASES.get((value or "").strip().lower(), "")
    if code in DECKS[deck]["langs"] or (value or "").strip().lower() == "auto":
        return code or "auto"
    return DECKS[deck]["input"]


# ---------- Base de données ----------
# Noms de colonnes historiques (app française) : word / sentence_fr = le mot et la phrase dans la
# langue du mot écrit (colonne lang), word_en / sentence_en = leurs traductions dans l'autre langue.

class DB:
    """Connexion SQLite (local) ou Postgres (Supabase), avec la même syntaxe « ? » partout."""

    def __init__(self):
        self.pg = bool(DATABASE_URL)
        if self.pg:
            import psycopg  # installé par requirements.txt ; inutile en local
            from psycopg.rows import dict_row
            # prepare_threshold=None : compatible avec le « pooler » de Supabase
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if self.pg:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, params)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()


def db():
    return DB()


def is_duplicate(exc):
    return isinstance(exc, sqlite3.IntegrityError) or type(exc).__name__ == "UniqueViolation"


# Colonnes ajoutées après coup : voix, suivi des exports, plusieurs langues, images, flashcards.
EXTRA_COLUMNS = (
    ("audio_word", "TEXT"), ("audio_sentence", "TEXT"),
    ("exported", "INTEGER NOT NULL DEFAULT 0"),  # 1 quand la carte est déjà partie dans un paquet Anki
    ("deck", "TEXT NOT NULL DEFAULT 'fr-en'"),
    ("lang", "TEXT NOT NULL DEFAULT 'fr'"),      # langue du mot écrit ("auto" en attendant Groq)
    ("image", "TEXT"),
    ("known", "INTEGER NOT NULL DEFAULT 0"),     # flashcards : 1 = glissée à droite (je sais)
)


def init_db():
    with db() as conn:
        if conn.pg:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS cards (
                    id BIGSERIAL PRIMARY KEY,
                    word TEXT NOT NULL,
                    context TEXT,
                    sentence_fr TEXT,
                    word_en TEXT,
                    sentence_en TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    tally_response_id TEXT UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )"""
            )
            for col, definition in EXTRA_COLUMNS:
                conn.execute(f"ALTER TABLE cards ADD COLUMN IF NOT EXISTS {col} {definition}")
            conn.execute("CREATE TABLE IF NOT EXISTS audio (name TEXT PRIMARY KEY, data BYTEA NOT NULL)")
        else:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    word TEXT NOT NULL,
                    context TEXT,
                    sentence_fr TEXT,
                    word_en TEXT,
                    sentence_en TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    tally_response_id TEXT UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(cards)")}
            for col, definition in EXTRA_COLUMNS:
                if col not in cols:
                    conn.execute(f"ALTER TABLE cards ADD COLUMN {col} {definition}")
            conn.execute("CREATE TABLE IF NOT EXISTS audio (name TEXT PRIMARY KEY, data BLOB NOT NULL)")
            # Anciennes versions : les mp3 étaient dans le dossier audio/, on les range dans la base.
            if os.path.isdir(AUDIO_DIR):
                for name in os.listdir(AUDIO_DIR):
                    if name.endswith(".mp3"):
                        with open(os.path.join(AUDIO_DIR, name), "rb") as f:
                            conn.execute("INSERT OR IGNORE INTO audio (name, data) VALUES (?, ?)",
                                         (name, f.read()))


# La table « audio » range tous les fichiers des cartes : mp3 et images.
def put_media(name, data):
    with db() as conn:
        conn.execute("DELETE FROM audio WHERE name = ?", (name,))
        conn.execute("INSERT INTO audio (name, data) VALUES (?, ?)", (name, data))


def get_media(name):
    with db() as conn:
        row = conn.execute("SELECT data FROM audio WHERE name = ?", (name,)).fetchone()
    return bytes(row["data"]) if row else None


def copy_local_to_online():
    """`python app.py copier` : envoie les mots et fichiers de words.db vers la base en ligne.

    À lancer une fois, avant d'ajouter des mots en ligne."""
    if not DATABASE_URL:
        raise SystemExit("Ajoute d'abord DATABASE_URL (Supabase) dans ton fichier .env.")
    init_db()
    local = sqlite3.connect(DB_PATH)
    local.row_factory = sqlite3.Row
    cards = local.execute("SELECT * FROM cards ORDER BY id").fetchall()
    has_audio_table = local.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audio'").fetchone()
    files = {r["name"]: r["data"] for r in local.execute("SELECT * FROM audio")} if has_audio_table else {}
    if os.path.isdir(AUDIO_DIR):  # mp3 encore dans le dossier audio/
        for name in os.listdir(AUDIO_DIR):
            if name.endswith(".mp3") and name not in files:
                with open(os.path.join(AUDIO_DIR, name), "rb") as f:
                    files[name] = f.read()
    # On garde les mêmes numéros de carte et noms de fichiers : Anki reconnaîtra les cartes déjà importées.
    copied, conflicts = 0, []
    with db() as conn:
        for r in cards:
            r = dict(r)
            existing = conn.execute("SELECT word FROM cards WHERE id = ?", (r["id"],)).fetchone()
            if existing:
                if existing["word"] != r["word"]:
                    conflicts.append(r["word"])
                continue
            conn.execute(
                """INSERT INTO cards (id, word, context, sentence_fr, word_en, sentence_en, status, error,
                                      audio_word, audio_sentence, deck, lang, image, known)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["id"], r["word"], r["context"], r["sentence_fr"], r["word_en"], r["sentence_en"],
                 r["status"], r["error"], r.get("audio_word"), r.get("audio_sentence"),
                 r.get("deck") or DEFAULT_DECK, r.get("lang") or "fr", r.get("image"), r.get("known") or 0))
            for name in (r.get("audio_word"), r.get("audio_sentence"), r.get("image")):
                if name and name in files:
                    conn.execute("INSERT INTO audio (name, data) VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
                                 (name, files[name]))
            copied += 1
        conn.execute("SELECT setval(pg_get_serial_sequence('cards', 'id'), (SELECT COALESCE(MAX(id), 1) FROM cards))")
    if conflicts:
        print("⚠️  Non copiés (la base en ligne a déjà une autre carte au même numéro) :", ", ".join(conflicts))
    print(f"{copied} carte(s) copiée(s) vers la base en ligne.")


# ---------- Groq ----------

PROMPT = """Tu aides quelqu'un à apprendre le {src}.
Mot ou expression en {src} : "{word}"
{context_line}
Réponds UNIQUEMENT avec un objet JSON contenant :
- "translation" : la traduction en {dst} du mot (le sens le plus courant, ou celui du contexte donné)
- "sentence" : une phrase d'exemple naturelle en {src} (niveau B1, 8 à 15 mots) qui utilise ce mot et en montre clairement le sens
- "sentence_translation" : la traduction complète de cette phrase en {dst}"""

PROMPT_AUTO = """Tu aides quelqu'un à apprendre le {a} et le {b}.
Mot ou expression : "{word}". Il est soit en {a}, soit en {b} : reconnais la langue.
{context_line}
Réponds UNIQUEMENT avec un objet JSON contenant :
- "lang" : "{a_code}" si le mot est en {a}, "{b_code}" s'il est en {b}
- "translation" : la traduction du mot dans l'autre langue (le sens le plus courant, ou celui du contexte donné)
- "sentence" : une phrase d'exemple naturelle dans la langue du mot (niveau B1, 8 à 15 mots) qui utilise ce mot et en montre clairement le sens
- "sentence_translation" : la traduction complète de cette phrase dans l'autre langue"""


# Modèles essayés si GROQ_MODEL n'existe plus chez Groq (les modèles changent souvent).
PREFERRED_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b",
                    "meta-llama/llama-4-maverick-17b-128e-instruct", "llama-3.1-8b-instant"]
_model = None


def groq_request(path, body=None):
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "language-anki-app/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def pick_available_model():
    """Demande à Groq la liste des modèles accessibles et en choisit un adapté."""
    ids = [m["id"] for m in groq_request("/models").get("data", [])
           if m.get("active", True)]
    for name in PREFERRED_MODELS:
        if name in ids:
            return name
    skip = ("whisper", "tts", "guard", "playai", "orpheus", "compound")
    candidates = [i for i in ids if not any(k in i.lower() for k in skip)]
    if not candidates:
        raise RuntimeError("Aucun modèle de texte disponible sur ton compte Groq.")
    return candidates[0]


def generate(word, context, deck, lang):
    """Renvoie {"lang", "translation", "sentence", "sentence_translation"}."""
    global _model
    if not GROQ_API_KEY:
        raise RuntimeError("La variable d'environnement GROQ_API_KEY n'est pas définie.")
    a, b = DECKS[deck]["langs"]
    context_line = f"Contexte ou sens voulu : {context}" if context else ""
    if lang == "auto":
        prompt = PROMPT_AUTO.format(word=word, context_line=context_line, a=LANGS[a][0], b=LANGS[b][0],
                                    a_code=a, b_code=b)
    else:
        other = b if lang == a else a
        prompt = PROMPT.format(word=word, context_line=context_line, src=LANGS[lang][0], dst=LANGS[other][0])
    body = {
        "model": _model or GROQ_MODEL,
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        try:
            data = groq_request("/chat/completions", body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code not in (400, 404) or "model" not in detail:
                raise RuntimeError(f"Groq a répondu {e.code} : {detail[:300]}")
            # Le modèle n'existe plus : on en choisit un autre automatiquement.
            _model = body["model"] = pick_available_model()
            print(f"Modèle Groq « {GROQ_MODEL} » indisponible, utilisation de « {_model} ».")
            data = groq_request("/chat/completions", body)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Groq a répondu {e.code} : {e.read().decode(errors='replace')[:300]}")
    result = json.loads(data["choices"][0]["message"]["content"])
    for key in ("translation", "sentence", "sentence_translation"):
        if not str(result.get(key, "")).strip():
            raise RuntimeError(f"Réponse Groq incomplète, champ manquant : {key}")
    if lang == "auto":
        lang = LANG_ALIASES.get(str(result.get("lang", "")).strip().lower(), a)
        lang = lang if lang in (a, b) else a
    result["lang"] = lang
    return result


def add_word(word, context="", deck=DEFAULT_DECK, lang=None, tally_response_id=None, process=True):
    """Enregistre le mot (et le traite, sauf process=False). Renvoie l'id de la carte."""
    word = word.strip()
    context = (context or "").strip()
    lang = lang or DECKS[deck]["input"]
    with db() as conn:
        try:
            card_id = conn.execute(
                "INSERT INTO cards (word, context, tally_response_id, deck, lang) VALUES (?, ?, ?, ?, ?) RETURNING id",
                (word, context, tally_response_id, deck, lang),
            ).fetchone()["id"]
        except Exception as e:
            if is_duplicate(e):
                return None  # Tally a renvoyé la même soumission : on l'ignore.
            raise
    if process:
        process_card(card_id)
    return card_id


def process_card(card_id):
    """Génère ce qui manque : le texte (Groq), puis l'audio (ElevenLabs), puis l'image si IMAGES_AUTO."""
    with db() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        return
    try:
        prefix = DECKS[deck_of(row["deck"])]["prefix"]
        if not row["sentence_fr"]:
            r = generate(row["word"], row["context"], deck_of(row["deck"]), row["lang"])
            with db() as conn:
                conn.execute(
                    "UPDATE cards SET sentence_fr=?, word_en=?, sentence_en=?, lang=? WHERE id=?",
                    (r["sentence"].strip(), r["translation"].strip(), r["sentence_translation"].strip(),
                     r["lang"], card_id),
                )
                row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if ELEVENLABS_API_KEY and not (row["audio_word"] and row["audio_sentence"]):
            audio_word = save_audio(row["word"], row["lang"], f"{prefix}_{card_id}_mot.mp3")
            audio_sentence = save_audio(row["sentence_fr"], row["lang"], f"{prefix}_{card_id}_phrase.mp3")
            with db() as conn:
                conn.execute("UPDATE cards SET audio_word=?, audio_sentence=? WHERE id=?",
                             (audio_word, audio_sentence, card_id))
        with db() as conn:
            conn.execute("UPDATE cards SET status='done', error=NULL WHERE id=?", (card_id,))
    except Exception as e:  # on garde le mot et on note l'erreur pour réessayer plus tard
        with db() as conn:
            conn.execute("UPDATE cards SET status='error', error=? WHERE id=?", (str(e), card_id))
        return
    if IMAGES_AUTO and images_enabled() and not row["image"]:
        try:
            save_image(card_id)
        except Exception as e:  # une image ratée n'empêche pas la carte d'exister
            print(f"Image de la carte {card_id} : {e}")


def in_background(func, *args):
    threading.Thread(target=func, args=args, daemon=True).start()


def process_many(ids):
    for card_id in ids:
        process_card(card_id)


# ---------- ElevenLabs ----------

_voice_id = None


def elevenlabs_request(path, body=None):
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json",
                 "User-Agent": "language-anki-app/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ElevenLabs a répondu {e.code} : {e.read().decode(errors='replace')[:300]}")


def voice_id(lang):
    """ELEVENLABS_VOICE_ID_<LANGUE> ou ELEVENLABS_VOICE_ID si définie, sinon une voix française du compte."""
    global _voice_id
    specific = os.environ.get(f"ELEVENLABS_VOICE_ID_{lang.upper()}", "")
    if specific or ELEVENLABS_VOICE_ID:
        return specific or ELEVENLABS_VOICE_ID
    if not _voice_id:
        voices = json.loads(elevenlabs_request("/voices")).get("voices", [])
        if not voices:
            raise RuntimeError("Aucune voix trouvée sur ton compte ElevenLabs.")
        def is_french(v):
            text = (v.get("name", "") + " " + json.dumps(v.get("labels") or {})).lower()
            return "french" in text or "fran" in text
        _voice_id = next((v for v in voices if is_french(v)), voices[0])["voice_id"]
    return _voice_id


def save_audio(text, lang, filename):
    """Lit le texte avec ElevenLabs (modèle multilingue) et enregistre le mp3. Renvoie le nom du fichier."""
    audio = elevenlabs_request(
        f"/text-to-speech/{voice_id(lang)}?output_format=mp3_44100_128",
        {"text": text, "model_id": ELEVENLABS_MODEL},
    )
    put_media(filename, audio)
    return filename


# ---------- Images IA ----------

def images_enabled():
    if IMAGE_PROVIDER == "openai":
        return bool(OPENAI_API_KEY)
    return IMAGE_PROVIDER == "pollinations"


def image_prompt(row):
    """Décrit le sens du mot en anglais (les générateurs d'images le comprennent le mieux)."""
    if row["lang"] == "en":
        word, sentence = row["word"], row["sentence_fr"]
    else:  # dans tous les paquets, l'autre langue est alors l'anglais
        word, sentence = row["word_en"], row["sentence_en"]
    return (f"Simple, clear, colorful flat illustration for a vocabulary flashcard, showing the meaning of "
            f"\"{word}\" as in: \"{sentence}\". No text, no letters, no words in the image.")


def fetch_image(prompt):
    """Renvoie les octets de l'image générée."""
    if IMAGE_PROVIDER == "openai":
        req = urllib.request.Request(
            "https://api.openai.com/v1/images/generations",
            data=json.dumps({"model": IMAGE_MODEL or "gpt-image-1-mini", "prompt": prompt,
                             "size": "1024x1024", "quality": "low", "n": 1}).encode(),
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                item = json.load(resp)["data"][0]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"OpenAI a répondu {e.code} : {e.read().decode(errors='replace')[:300]}")
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        with urllib.request.urlopen(item["url"], timeout=120) as resp:
            return resp.read()
    query = urllib.parse.urlencode({"width": 512, "height": 512, "nologo": "true",
                                    "seed": random.randint(1, 10**9), **({"model": IMAGE_MODEL} if IMAGE_MODEL else {})})
    headers = {"User-Agent": "language-anki-app/1.0"}
    if POLLINATIONS_API_KEY:
        headers["Authorization"] = f"Bearer {POLLINATIONS_API_KEY}"
    req = urllib.request.Request(
        "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt, safe="") + "?" + query,
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Pollinations a répondu {e.code} : {e.read().decode(errors='replace')[:300]}")


def image_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise RuntimeError("Le générateur d'images n'a pas renvoyé d'image.")


def save_image(card_id):
    """Génère (ou régénère) l'image d'une carte terminée. Renvoie le nom du fichier."""
    with db() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row or row["status"] != "done":
        return None
    data = fetch_image(image_prompt(row))
    name = f"{DECKS[deck_of(row['deck'])]['prefix']}_{card_id}_image.{image_ext(data)}"
    put_media(name, data)
    with db() as conn:
        if row["image"] and row["image"] != name:
            conn.execute("DELETE FROM audio WHERE name = ?", (row["image"],))
        conn.execute("UPDATE cards SET image=? WHERE id=?", (name, card_id))
    return name


_images_running = threading.Lock()


def add_missing_images(deck):
    """En arrière-plan : une image pour chaque carte du paquet qui n'en a pas."""
    if not _images_running.acquire(blocking=False):
        return  # déjà en cours
    try:
        with db() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM cards WHERE deck=? AND status='done' AND image IS NULL ORDER BY id", (deck,))]
        for card_id in ids:
            try:
                save_image(card_id)
            except Exception as e:
                print(f"Image de la carte {card_id} : {e}")
    finally:
        _images_running.release()


# ---------- Tally ----------

def verify_tally_signature(raw_body, signature):
    if not TALLY_SIGNING_SECRET:
        return True
    expected = base64.b64encode(
        hmac.new(TALLY_SIGNING_SECRET.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature or "")


def field_text(field):
    value = field.get("value")
    if isinstance(value, list):  # listes à choix : on prend les libellés des options
        options = {o.get("id"): o.get("text") for o in field.get("options", [])}
        return ", ".join(str(options.get(v, v)) for v in value)
    return "" if value is None else str(value)


def split_word(entry):
    """« avocat (le fruit) » -> ("avocat", "le fruit"). Sans parenthèses : (entry, "")."""
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", entry.strip())
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    return entry.strip(), ""


def pair_words(word_text, context_text=""):
    """Un mot par ligne ; chaque mot peut avoir son contexte entre parenthèses.

    Le champ Contexte s'applique ligne par ligne s'il a autant de lignes que de mots,
    ou au mot unique s'il n'y en a qu'un. Les parenthèses sont prioritaires.
    """
    entries = [split_word(line) for line in word_text.splitlines() if line.strip()]
    ctx_lines = [c.strip() for c in (context_text or "").splitlines() if c.strip()]
    if len(entries) == 1:
        ctx_lines = [" ".join(ctx_lines)] if ctx_lines else []
    pairs = []
    for i, (word, ctx) in enumerate(entries):
        if not ctx and len(ctx_lines) == len(entries):
            ctx = ctx_lines[i]
        pairs.append((word, ctx))
    return pairs


def parse_tally(payload):
    """Renvoie (liste de (mot, contexte), id de la réponse, langue indiquée ou "")."""
    data = payload.get("data", {})
    fields = data.get("fields", [])
    word, context, lang = "", "", ""
    for f in fields:
        label = (f.get("label") or "").strip().lower()
        if label in WORD_LABELS and not word:
            word = field_text(f)
        elif label in CONTEXT_LABELS and not context:
            context = field_text(f)
        elif label in LANG_LABELS and not lang:
            lang = field_text(f)
    if not word:  # sinon : premier champ texte
        for f in fields:
            if f.get("type") in ("INPUT_TEXT", "TEXTAREA") and f.get("value"):
                word = field_text(f)
                break
    return pair_words(word, context), data.get("responseId"), lang


# ---------- Export Anki ----------

def done_cards(deck, new_only=False):
    sql = "SELECT * FROM cards WHERE status='done' AND deck=?" + (" AND exported = 0" if new_only else "")
    with db() as conn:
        return conn.execute(sql + " ORDER BY id", (deck,)).fetchall()


def mark_exported(ids):
    with db() as conn:
        for card_id in ids:
            conn.execute("UPDATE cards SET exported = 1 WHERE id = ?", (card_id,))


def delete_cards(ids):
    """Efface les cartes et leurs fichiers de la base (pas d'Anki)."""
    with db() as conn:
        for card_id in ids:
            row = conn.execute("SELECT audio_word, audio_sentence, image FROM cards WHERE id = ?",
                               (card_id,)).fetchone()
            if not row:
                continue
            for name in (row["audio_word"], row["audio_sentence"], row["image"]):
                if name:
                    conn.execute("DELETE FROM audio WHERE name = ?", (name,))
            conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))


def media_names(r):
    return [n for n in (r["audio_word"], r["audio_sentence"], r["image"]) if n]


def sound(filename):
    return f"[sound:{filename}]" if filename else ""


def front_back(r):
    """Recto = le mot écrit et sa phrase (avec audio), verso = traductions (+ image)."""
    e = lambda t: html.escape(t, quote=False)
    front = (f"<b>{e(r['word'])}</b> {sound(r['audio_word'])}<br><br>"
             f"<i>{e(r['sentence_fr'])}</i> {sound(r['audio_sentence'])}")
    back = f"<b>{e(r['word_en'])}</b><br><br><i>{e(r['sentence_en'])}</i>"
    if r["image"]:
        back += f'<br><br><img src="{html.escape(r["image"])}">'
    return front, back


def export_csv(deck):
    out = io.StringIO()
    # En-têtes reconnus par Anki (2.1.54+) : séparateur, HTML activé, noms de colonnes.
    out.write("#separator:Comma\n#html:true\n#columns:Front,Back,Tags\n#tags column:3\n")
    w = csv.writer(out)
    for r in done_cards(deck):
        front, back = front_back(r)
        w.writerow([front, back, DECKS[deck]["tag"]])
    return out.getvalue()


def export_media_zip(deck):
    """Les mp3 et images, à copier dans le dossier collection.media d'Anki si on importe le CSV."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for r in done_cards(deck):
            for name in media_names(r):
                data = get_media(name)
                if data:
                    z.writestr(name, data)
    return buf.getvalue()


def export_apkg(deck, new_only=True):
    """Paquet Anki (cartes + audio + images) : un double-clic suffit. Nécessite `pip install genanki`.

    Par défaut, seulement les cartes pas encore exportées. Renvoie (contenu, ids des cartes)."""
    import genanki  # importé ici pour que le reste de l'app marche sans
    d = DECKS[deck]
    model = genanki.Model(
        d["model"], "Mots français (audio)" if deck == "fr-en" else "Mots (audio, image)",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[{"name": "Carte 1", "qfmt": "{{Front}}",
                    "afmt": "{{FrontSide}}<hr id=answer>{{Back}}"}],
        css=(".card{font-family:arial;font-size:22px;text-align:center;color:black;background:white}"
             "img{max-width:100%;max-height:300px}"),
    )
    anki_deck = genanki.Deck(d["anki_deck"], d["anki_name"])
    media = []
    tmpdir = tempfile.mkdtemp()  # genanki lit les fichiers depuis le disque
    cards = done_cards(deck, new_only)
    for r in cards:
        front, back = front_back(r)
        # guid stable : réimporter le paquet met à jour les cartes au lieu de les dupliquer
        anki_deck.add_note(genanki.Note(model=model, fields=[front, back], tags=[d["tag"]],
                                        guid=genanki.guid_for(d["prefix"], r["id"])))
        for name in media_names(r):
            data = get_media(name)
            if data:
                with open(os.path.join(tmpdir, name), "wb") as f:
                    f.write(data)
                media.append(os.path.join(tmpdir, name))
    package = genanki.Package(anki_deck)
    package.media_files = media
    path = os.path.join(tmpdir, "paquet.apkg")
    try:
        package.write_to_file(path)
        with open(path, "rb") as f:
            return f.read(), [r["id"] for r in cards]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- Serveur web ----------

STYLE = """
body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#222}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top}
.err{color:#b00} a.btn,button{background:#2a5bd7;color:#fff;border:0;padding:.5rem 1rem;border-radius:6px;text-decoration:none;cursor:pointer;font:inherit}
input,select{padding:.45rem;border:1px solid #bbb;border-radius:6px;font:inherit} form{margin:1rem 0}
button.danger{background:#b3261e} button.x{background:none;color:#999;padding:.2rem .4rem;font-size:1.1rem}
button.light{background:#eef2fb;color:#2a5bd7}
tr.exported td{color:#888} .small{color:#666;font-size:.9rem} .tag{font-size:.75rem;color:#2e7d32}
.tabs{display:flex;gap:.4rem;flex-wrap:wrap;margin:1rem 0}
.tabs a{padding:.4rem .9rem;border-radius:999px;background:#eef2fb;color:#2a5bd7;text-decoration:none}
.tabs a.on{background:#2a5bd7;color:#fff}
.lang{font-size:.7rem;color:#666;border:1px solid #ccc;border-radius:4px;padding:0 .25rem;margin-right:.3rem}
td img{width:72px;height:72px;object-fit:cover;border-radius:6px;display:block}
a.cards{background:#2e7d32}
"""

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mes mots</title><style>{style}</style></head><body>
<h1>Mes mots</h1>
<nav class=tabs>{tabs}</nav>
<p>{download} <a class="btn cards" href="/cartes?deck={deck}">Réviser en flashcards</a></p>
<p class=small>{count} carte(s) dans ce paquet, dont {exported} déjà téléchargée(s).
<a href="/export.apkg?deck={deck}&amp;tout=1">Retélécharger toutes les cartes</a> ·
<a href="/export.csv?deck={deck}">CSV</a> · <a href="/audio.zip?deck={deck}">fichiers audio et images (.zip)</a></p>
<form method="post" action="/add">
<input type="hidden" name="deck" value="{deck}">
<input name="word" placeholder="{placeholder}" required>
{lang_select}
<input name="context" placeholder="contexte (optionnel)">
<button>Ajouter</button></form>
<form method="post" action="/retry" style="display:inline"><input type="hidden" name="deck" value="{deck}"><button>Réessayer les erreurs / ajouter l'audio manquant</button></form>
{images_button}
<form method="post" action="/delete-exported" style="display:inline"
 onsubmit="return confirm('Effacer de l’app tous les mots déjà téléchargés de ce paquet ? Ils restent dans Anki.')">
<input type="hidden" name="deck" value="{deck}"><button class=danger>Effacer les mots déjà téléchargés</button></form>
<table><tr><th>Mot</th><th>Phrase</th><th>Traduction</th><th>Image</th><th></th></tr>{rows}</table>
</body></html>"""


def render_home(deck):
    d = DECKS[deck]
    a, b = d["langs"]
    with db() as conn:
        rows = conn.execute("SELECT * FROM cards WHERE deck=? ORDER BY id DESC", (deck,)).fetchall()
    e = html.escape
    pair = d["input"] == "auto"
    trs = []
    for r in rows:
        hidden = f'<input type="hidden" name="id" value="{r["id"]}"><input type="hidden" name="deck" value="{deck}">'
        delete = (f'<form method="post" action="/delete" style="margin:0" '
                  f'onsubmit="return confirm(\'Effacer ce mot de l’app ?\')">'
                  f'{hidden}<button class=x title="Effacer">✕</button></form>')
        label = f'<span class=lang>{e(r["lang"].upper())}</span>' if pair else ""
        if r["status"] == "done":
            audio = "".join(f'<br><audio controls preload="none" src="/media/{e(n)}"></audio>'
                            for n in (r["audio_word"], r["audio_sentence"]) if n)
            tag = "<br><span class=tag>✓ dans Anki</span>" if r["exported"] else ""
            image = f'<img src="/media/{e(r["image"])}" alt="" loading="lazy">' if r["image"] else ""
            if images_enabled():
                image += (f'<form method="post" action="/image" style="margin:.3rem 0">{hidden}'
                          f'<button class="x" title="{"Nouvelle image" if r["image"] else "Générer une image"}">'
                          f'{"↻" if r["image"] else "🖼"}</button></form>')
            trs.append(f"<tr class={'exported' if r['exported'] else 'new'}><td>{label}<b>{e(r['word'])}</b>{tag}</td>"
                       f"<td>{e(r['sentence_fr'])}{audio}</td>"
                       f"<td><b>{e(r['word_en'])}</b><br>{e(r['sentence_en'])}</td><td>{image}</td><td>{delete}</td></tr>")
        else:
            msg = e(r["error"] or "en cours…")
            trs.append(f"<tr><td>{label}<b>{e(r['word'])}</b></td><td colspan=3 class=err>{msg}</td><td>{delete}</td></tr>")
    done = [r for r in rows if r["status"] == "done"]
    exported = sum(1 for r in done if r["exported"])
    new = len(done) - exported
    if new:
        download = (f'<a class="btn" href="/export.apkg?deck={deck}">Télécharger les {new} nouvelle(s) carte(s) '
                    f'pour Anki</a>')
    else:
        download = "Aucune nouvelle carte à télécharger."
    tabs = "".join(f'<a href="/?deck={k}" class="{"on" if k == deck else ""}">{e(v["title"])}</a>'
                   for k, v in DECKS.items())
    options = [("auto", "langue : auto")] if pair else []
    options += [(a, LANGS[a][1]), (b, LANGS[b][1])]
    default = d["input"]
    lang_select = ('<select name="lang" title="Langue du mot écrit">'
                   + "".join(f'<option value="{k}"{" selected" if k == default else ""}>{e(t)}</option>'
                             for k, t in options) + "</select>")
    missing = sum(1 for r in done if not r["image"])
    images_button = (f'<form method="post" action="/images" style="display:inline">'
                     f'<input type="hidden" name="deck" value="{deck}">'
                     f'<button class=light>Ajouter les images manquantes ({missing})</button></form>'
                     if images_enabled() and missing else "")
    placeholder = "mot" if pair else f"mot en {LANGS[a][0]}"
    return PAGE.format(style=STYLE, tabs=tabs, deck=deck, download=download, count=len(done),
                       exported=exported, rows="".join(trs), lang_select=lang_select,
                       images_button=images_button, placeholder=placeholder)


def card_json(r, deck):
    """Une carte pour la page flashcards : le texte de chaque langue du paquet."""
    a, b = DECKS[deck]["langs"]
    src = {"w": r["word"], "s": r["sentence_fr"], "aw": r["audio_word"], "as": r["audio_sentence"]}
    dst = {"w": r["word_en"], "s": r["sentence_en"]}
    sides = {a: src, b: dst} if r["lang"] == a else {a: dst, b: src}
    return {"id": r["id"], "known": bool(r["known"]), "img": r["image"], "sides": sides}


FLASHCARDS = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Flashcards</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--ink:#1d2433;--muted:#6b7385;--blue:#2a5bd7;--green:#2e7d32;--orange:#d9730d}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--ink);min-height:100vh;
 display:flex;flex-direction:column;align-items:center;padding:1rem;overflow-x:hidden}
header{width:100%;max-width:560px;display:flex;justify-content:space-between;align-items:center;gap:.5rem;flex-wrap:wrap}
header a{color:var(--blue);text-decoration:none}
select,button{font:inherit}
.bar{width:100%;max-width:560px;display:flex;gap:.4rem;flex-wrap:wrap;margin:.8rem 0}
.bar button{border:0;border-radius:999px;padding:.4rem .8rem;background:#e3e9f8;color:var(--blue);cursor:pointer}
.bar button.on{background:var(--blue);color:#fff}
.score{width:100%;max-width:560px;display:flex;justify-content:space-between;align-items:center;font-weight:600}
.pill{border-radius:999px;padding:.2rem .7rem;border:2px solid}
.pill.l{color:var(--orange);border-color:var(--orange)} .pill.r{color:var(--green);border-color:var(--green)}
#stage{position:relative;width:100%;max-width:560px;height:min(62vh,440px);margin:1rem 0;perspective:1400px;touch-action:pan-y}
.card{position:absolute;inset:0;cursor:grab;user-select:none;-webkit-user-select:none;transition:transform .25s ease}
.card.drag{transition:none;cursor:grabbing}
.inner{position:absolute;inset:0;transition:transform .45s;transform-style:preserve-3d}
.card.flipped .inner{transform:rotateY(180deg)}
.face{position:absolute;inset:0;background:var(--card);border-radius:18px;box-shadow:0 8px 28px rgba(20,30,60,.13);
 backface-visibility:hidden;-webkit-backface-visibility:hidden;display:flex;flex-direction:column;
 align-items:center;justify-content:center;text-align:center;padding:1.4rem;gap:.8rem;overflow:hidden}
.face.back{transform:rotateY(180deg)}
.lang{position:absolute;top:.8rem;left:1rem;font-size:.8rem;color:var(--muted);letter-spacing:.05em}
.word{font-size:clamp(1.6rem,6vw,2.4rem);font-weight:700}
.sent{font-size:clamp(1rem,3.6vw,1.2rem);color:#3b4252;font-style:italic;max-width:30em}
.face img{max-width:100%;max-height:45%;border-radius:12px;object-fit:contain}
.play{border:0;background:#e3e9f8;color:var(--blue);border-radius:999px;width:2.2rem;height:2.2rem;cursor:pointer;
 font-size:.9rem;vertical-align:middle;margin-left:.3rem}
.stamp{position:absolute;top:1.2rem;font-weight:800;font-size:1.3rem;padding:.2rem .7rem;border:3px solid;
 border-radius:8px;opacity:0;pointer-events:none;z-index:2}
.stamp.l{right:1.2rem;color:var(--orange);transform:rotate(12deg)}
.stamp.r{left:1.2rem;color:var(--green);transform:rotate(-12deg)}
.hint{font-size:.8rem;color:var(--muted);position:absolute;bottom:.8rem}
.actions{display:flex;gap:1rem;align-items:center}
.actions button{border:0;border-radius:999px;cursor:pointer}
.big{width:4rem;height:4rem;font-size:1.6rem;color:#fff}
.no{background:var(--orange)} .yes{background:var(--green)}
.undo{background:#e3e9f8;color:var(--blue);padding:.6rem 1rem}
.end{text-align:center;max-width:560px}
.end button{border:0;border-radius:999px;padding:.7rem 1.2rem;margin:.3rem;cursor:pointer;background:var(--blue);color:#fff}
.keys{color:var(--muted);font-size:.8rem;margin-top:1rem;text-align:center}
</style></head><body>
<header><a href="/?deck=%%DECK%%">← Mes mots</a><select id=deck>%%DECKS%%</select></header>
<div class=bar>
 <button id=swap title="Échanger recto et verso">⇄ Recto : <span id=frontName></span></button>
 <button id=shuffle>🔀 Mélanger</button>
 <button id=onlyNew>Seulement les non sues</button>
 <button id=autoplay>🔊 Lecture auto</button>
</div>
<div class=score><span class="pill l" id=nLeft>0</span><span id=progress></span><span class="pill r" id=nRight>0</span></div>
<div id=stage></div>
<div class=actions id=actions>
 <button class="big no" id=btnNo title="À revoir (←)">✗</button>
 <button class=undo id=btnUndo title="Annuler (Retour arrière)">↶</button>
 <button class="big yes" id=btnYes title="Je sais (→)">✓</button>
</div>
<p class=keys>Touche la carte pour la retourner · glisse à droite si tu sais, à gauche sinon<br>
Clavier : espace = retourner, → = je sais, ← = à revoir, retour arrière = annuler</p>
<script>
const DECK = "%%DECK%%", LANGS = %%LANGS%%, ALL = %%CARDS%%;
const store = {get(k, d){try{const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v)}catch(e){return d}},
               set(k, v){try{localStorage.setItem(k, JSON.stringify(v))}catch(e){}}};
const opt = {front: store.get("front:" + DECK, LANGS[0][0]), shuffle: store.get("shuffle", true),
             onlyNew: store.get("onlyNew", false), autoplay: store.get("autoplay", false)};
if (!LANGS.some(l => l[0] === opt.front)) opt.front = LANGS[0][0];
let queue = [], pos = 0, left = [], right = [], history = [];
const $ = id => document.getElementById(id), stage = $("stage");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const langName = code => LANGS.find(l => l[0] === code)[1];
const backLang = () => LANGS.find(l => l[0] !== opt.front)[0];

function play(name){ if (name) { const a = new Audio("/media/" + encodeURIComponent(name)); a.play().catch(() => {}); } }

function face(card, lang, isBack){
  const s = card.sides[lang];
  const btn = n => n ? `<button class=play data-audio="${esc(n)}" title="Écouter">▶</button>` : "";
  const img = isBack && card.img ? `<img src="/media/${encodeURIComponent(card.img)}" alt="">` : "";
  return `<span class=lang>${esc(langName(lang))}</span>
    <div class=word>${esc(s.w)}${btn(s.aw)}</div><div class=sent>${esc(s.s)}${btn(s.as)}</div>${img}
    ${isBack ? "" : "<span class=hint>touche pour retourner</span>"}`;
}

function start(cards){
  queue = cards.slice();
  if (opt.shuffle) for (let i = queue.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [queue[i], queue[j]] = [queue[j], queue[i]]; }
  pos = 0; left = []; right = []; history = [];
  show();
}

function deckCards(){ return opt.onlyNew ? ALL.filter(c => !c.known) : ALL; }

function updateBar(){
  $("frontName").textContent = langName(opt.front);
  $("shuffle").classList.toggle("on", opt.shuffle);
  $("onlyNew").classList.toggle("on", opt.onlyNew);
  $("autoplay").classList.toggle("on", opt.autoplay);
  $("nLeft").textContent = left.length; $("nRight").textContent = right.length;
  $("progress").textContent = queue.length ? `${Math.min(pos + 1, queue.length)} / ${queue.length}` : "";
}

function show(){
  updateBar();
  stage.innerHTML = "";
  $("actions").style.visibility = pos < queue.length ? "visible" : "hidden";
  if (!queue.length) {
    stage.innerHTML = `<div class=end><h2>Aucune carte ici</h2><p>${ALL.length ? "Tu connais déjà toutes les cartes de ce paquet. 🎉" : "Ajoute des mots sur la page « Mes mots »."}</p>
      ${ALL.length ? '<button id=all>Revoir toutes les cartes</button>' : ""}</div>`;
    const b = $("all"); if (b) b.onclick = () => { opt.onlyNew = false; store.set("onlyNew", false); start(ALL); };
    return;
  }
  if (pos >= queue.length) {
    stage.innerHTML = `<div class=end><h2>Fini !</h2><p>✓ ${right.length} sue(s) · ✗ ${left.length} à revoir</p>
      ${left.length ? `<button id=again>Revoir les ${left.length} carte(s) à revoir</button>` : ""}
      <button id=restart>Tout recommencer</button></div>`;
    if (left.length) $("again").onclick = () => start(left);
    $("restart").onclick = () => start(deckCards());
    return;
  }
  const card = queue[pos];
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `<div class=inner><div class="face front">${face(card, opt.front, false)}</div>
    <div class="face back">${face(card, backLang(), true)}</div></div>
    <div class="stamp r">JE SAIS</div><div class="stamp l">À REVOIR</div>`;
  stage.appendChild(el);
  drag(el);
  if (opt.autoplay) play(card.sides[opt.front].aw);
}

function decide(known){
  if (pos >= queue.length) return;
  const card = queue[pos], el = stage.querySelector(".card");
  history.push({card, was: card.known, known});
  card.known = known;
  (known ? right : left).push(card);
  fetch("/api/review", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id: card.id, known})}).catch(() => {});
  if (el) { el.style.transform = `translateX(${known ? 130 : -130}%) rotate(${known ? 18 : -18}deg)`; el.style.opacity = 0; }
  pos++;
  setTimeout(show, 220);
}

function undo(){
  const last = history.pop();
  if (!last) return;
  const pile = last.known ? right : left;
  pile.splice(pile.lastIndexOf(last.card), 1);
  last.card.known = last.was;
  fetch("/api/review", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id: last.card.id, known: last.was})}).catch(() => {});
  pos--;
  show();
}

function drag(el){
  let x0 = null, y0 = 0, dx = 0, moved = false;
  el.addEventListener("pointerdown", ev => {
    if (ev.target.closest(".play")) return;
    x0 = ev.clientX; y0 = ev.clientY; dx = 0; moved = false;
    el.setPointerCapture(ev.pointerId); el.classList.add("drag");
  });
  el.addEventListener("pointermove", ev => {
    if (x0 === null) return;
    dx = ev.clientX - x0;
    if (Math.abs(dx) > 6 || Math.abs(ev.clientY - y0) > 6) moved = true;
    el.style.transform = `translateX(${dx}px) rotate(${dx / 18}deg)`;
    el.querySelector(".stamp.r").style.opacity = Math.max(0, Math.min(1, dx / 110));
    el.querySelector(".stamp.l").style.opacity = Math.max(0, Math.min(1, -dx / 110));
  });
  const end = () => {
    if (x0 === null) return;
    x0 = null; el.classList.remove("drag");
    if (Math.abs(dx) > 100) return decide(dx > 0);
    el.style.transform = "";
    el.querySelectorAll(".stamp").forEach(s => s.style.opacity = 0);
    if (!moved) el.classList.toggle("flipped");
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", () => { dx = 0; moved = true; end(); });
}

stage.addEventListener("click", ev => { const b = ev.target.closest(".play"); if (b) { ev.stopPropagation(); play(b.dataset.audio); } });
$("btnYes").onclick = () => decide(true);
$("btnNo").onclick = () => decide(false);
$("btnUndo").onclick = undo;
$("swap").onclick = () => { opt.front = backLang(); store.set("front:" + DECK, opt.front); show(); };
$("shuffle").onclick = () => { opt.shuffle = !opt.shuffle; store.set("shuffle", opt.shuffle); start(deckCards()); };
$("onlyNew").onclick = () => { opt.onlyNew = !opt.onlyNew; store.set("onlyNew", opt.onlyNew); start(deckCards()); };
$("autoplay").onclick = () => { opt.autoplay = !opt.autoplay; store.set("autoplay", opt.autoplay); updateBar(); };
$("deck").onchange = ev => { location.href = "/cartes?deck=" + ev.target.value; };
document.addEventListener("keydown", ev => {
  if (ev.target.tagName === "SELECT") return;
  if (ev.key === "ArrowRight") decide(true);
  else if (ev.key === "ArrowLeft") decide(false);
  else if (ev.key === " " || ev.key === "ArrowUp" || ev.key === "ArrowDown") { ev.preventDefault(); const c = stage.querySelector(".card"); if (c) c.classList.toggle("flipped"); }
  else if (ev.key === "Backspace") { ev.preventDefault(); undo(); }
});
start(deckCards());
</script></body></html>"""


def render_flashcards(deck):
    with db() as conn:
        rows = conn.execute("SELECT * FROM cards WHERE deck=? AND status='done' ORDER BY id DESC", (deck,)).fetchall()
    dump = lambda v: json.dumps(v, ensure_ascii=False).replace("</", "<\\/")
    langs = [[code, LANGS[code][1]] for code in DECKS[deck]["langs"]]
    options = "".join(f'<option value="{k}"{" selected" if k == deck else ""}>{html.escape(v["title"])}</option>'
                      for k, v in DECKS.items())
    return (FLASHCARDS.replace("%%DECKS%%", options).replace("%%LANGS%%", dump(langs))
            .replace("%%CARDS%%", dump([card_json(r, deck) for r in rows])).replace("%%DECK%%", deck))


MEDIA_TYPES = {".mp3": "audio/mpeg", ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location="/"):
        self.send(303, "", extra={"Location": location})

    def read_body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def authorized(self):
        """Mot de passe demandé par le navigateur (identifiant au choix). Le webhook Tally en est exempté."""
        if not APP_PASSWORD:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
            except ValueError:
                pwd = ""
            if hmac.compare_digest(pwd.encode(), APP_PASSWORD.encode()):
                return True
        self.send(401, "Mot de passe requis", "text/plain; charset=utf-8",
                  {"WWW-Authenticate": 'Basic realm="Mes mots"'})
        return False

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        deck = deck_of(query.get("deck", [""])[0])
        tag = DECKS[deck]["tag"]
        if not self.authorized():
            return
        if path == "/":
            self.send(200, render_home(deck))
        elif path == "/cartes":
            self.send(200, render_flashcards(deck))
        elif path == "/export.csv":
            self.send(200, export_csv(deck).encode("utf-8"), "text/csv; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="anki_{tag}.csv"'})
        elif path == "/export.apkg":
            try:
                data, ids = export_apkg(deck, new_only="tout" not in query)
            except ImportError:
                return self.send(500, "Pour le paquet Anki, installe genanki : python -m pip install genanki",
                                 "text/plain; charset=utf-8")
            self.send(200, data, "application/octet-stream",
                      {"Content-Disposition": f'attachment; filename="{tag}.apkg"'})
            mark_exported(ids)  # la prochaine fois, seulement les nouvelles cartes
        elif path == "/audio.zip":
            self.send(200, export_media_zip(deck), "application/zip",
                      {"Content-Disposition": f'attachment; filename="fichiers_{tag}.zip"'})
        elif path.startswith("/media/") or path.startswith("/audio/"):
            name = os.path.basename(urllib.parse.unquote(path))
            ctype = MEDIA_TYPES.get(os.path.splitext(name)[1])
            data = get_media(name) if ctype else None
            if not data:
                return self.send(404, "Page introuvable")
            self.send(200, data, ctype, {"Cache-Control": "private, max-age=86400"})
        else:
            self.send(404, "Page introuvable")

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        raw = self.read_body()
        if path != "/webhook/tally" and not self.authorized():
            return
        if path == "/webhook/tally":
            if not verify_tally_signature(raw, self.headers.get("Tally-Signature")):
                return self.send(401, "Signature invalide", "text/plain")
            try:
                pairs, response_id, field_lang = parse_tally(json.loads(raw))
            except (ValueError, AttributeError):
                return self.send(400, "JSON invalide", "text/plain")
            query = urllib.parse.parse_qs(url.query)
            deck = deck_of(query.get("deck", [""])[0])
            lang = lang_of(field_lang or query.get("lang", [""])[0], deck)
            ids = []
            for i, (w, ctx) in enumerate(pairs):
                rid = f"{response_id}:{i}" if response_id else None
                card_id = add_word(w, ctx, deck, lang, rid, process=False)
                if card_id:
                    ids.append(card_id)
            in_background(process_many, ids)  # Tally n'attend pas Groq, ElevenLabs et les images
            self.send(200, json.dumps({"ok": True, "words": [w for w, _ in pairs]}), "application/json")
            return
        if path == "/api/review":
            try:
                body = json.loads(raw)
                card_id, known = int(body["id"]), 1 if body.get("known") else 0
            except (ValueError, KeyError, TypeError):
                return self.send(400, "JSON invalide", "text/plain")
            with db() as conn:
                conn.execute("UPDATE cards SET known=? WHERE id=?", (known, card_id))
            return self.send(200, '{"ok":true}', "application/json")
        form = urllib.parse.parse_qs(raw.decode())
        deck = deck_of(form.get("deck", [""])[0])
        card_id = form.get("id", [""])[0]
        if path == "/add":
            word = form.get("word", [""])[0]
            if word.strip():
                lang = lang_of(form.get("lang", [""])[0], deck)
                for w, ctx in pair_words(word, form.get("context", [""])[0]):
                    add_word(w, ctx, deck, lang)
        elif path == "/delete":
            if card_id.isdigit():
                delete_cards([int(card_id)])
        elif path == "/delete-exported":
            delete_cards([r["id"] for r in done_cards(deck) if r["exported"]])
        elif path == "/retry":
            with db() as conn:
                query = "SELECT id FROM cards WHERE deck=? AND (status='error'"
                if ELEVENLABS_API_KEY:
                    query += " OR audio_word IS NULL OR audio_sentence IS NULL"
                ids = [r["id"] for r in conn.execute(query + ")", (deck,))]
            process_many(ids)
        elif path == "/image":
            if card_id.isdigit():
                try:
                    save_image(int(card_id))
                except Exception as e:
                    return self.send(502, f"Image non générée : {e}", "text/plain; charset=utf-8")
        elif path == "/images":
            in_background(add_missing_images, deck)
        else:
            return self.send(404, "Page introuvable")
        self.redirect(f"/?deck={deck}")


if __name__ == "__main__":
    if sys.argv[1:] == ["copier"]:
        copy_local_to_online()
        raise SystemExit
    init_db()
    if not GROQ_API_KEY:
        print("⚠️  GROQ_API_KEY n'est pas définie : les phrases ne pourront pas être générées.")
    if not ELEVENLABS_API_KEY:
        print("ℹ️  ELEVENLABS_API_KEY n'est pas définie : les cartes seront créées sans audio.")
    print("Images IA :", IMAGE_PROVIDER if images_enabled() else "désactivées",
          "(automatiques)" if IMAGES_AUTO and images_enabled() else "")
    print("Base :", "Supabase (en ligne)" if DATABASE_URL else DB_PATH)
    print(f"App lancée sur http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
