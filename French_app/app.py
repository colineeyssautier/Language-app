"""Petite app : Tally -> Groq (+ voix ElevenLabs) -> SQLite -> paquet Anki.

Bibliothèque standard de Python, plus `genanki` pour l'export .apkg (python -m pip install genanki).

Lancer :  python3 app.py
Pages :
  GET  /             page d'accueil (liste des mots + ajout manuel)
  POST /add          ajout manuel d'un mot (formulaire de la page d'accueil)
  POST /webhook/tally   reçoit les soumissions Tally
  GET  /export.apkg  paquet Anki avec l'audio (recommandé)
  GET  /export.csv   CSV pour Anki (l'audio est à copier à part : /audio.zip)
"""

import base64
import csv
import hashlib
import hmac
import html
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
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
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
AUDIO_DIR = os.environ.get("AUDIO_DIR", os.path.join(os.path.dirname(DB_PATH), "audio"))

# Libellés des champs du formulaire Tally (insensible à la casse).
WORD_LABELS = {"mot", "mot en français", "word", "french word"}
CONTEXT_LABELS = {"contexte", "context", "note"}


# ---------- Base de données ----------

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
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    audio_word TEXT,
                    audio_sentence TEXT
                )"""
            )
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
            for col in ("audio_word", "audio_sentence"):  # ajoutées avec la voix ElevenLabs
                if col not in cols:
                    conn.execute(f"ALTER TABLE cards ADD COLUMN {col} TEXT")
            conn.execute("CREATE TABLE IF NOT EXISTS audio (name TEXT PRIMARY KEY, data BLOB NOT NULL)")
            # Anciennes versions : les mp3 étaient dans le dossier audio/, on les range dans la base.
            if os.path.isdir(AUDIO_DIR):
                for name in os.listdir(AUDIO_DIR):
                    if name.endswith(".mp3"):
                        with open(os.path.join(AUDIO_DIR, name), "rb") as f:
                            conn.execute("INSERT OR IGNORE INTO audio (name, data) VALUES (?, ?)",
                                         (name, f.read()))


def put_audio(name, data):
    with db() as conn:
        conn.execute("DELETE FROM audio WHERE name = ?", (name,))
        conn.execute("INSERT INTO audio (name, data) VALUES (?, ?)", (name, data))


def get_audio(name):
    with db() as conn:
        row = conn.execute("SELECT data FROM audio WHERE name = ?", (name,)).fetchone()
    return bytes(row["data"]) if row else None


def copy_local_to_online():
    """`python app.py copier` : envoie les mots et audios de words.db vers la base en ligne.

    À lancer une fois, avant d'ajouter des mots en ligne."""
    if not DATABASE_URL:
        raise SystemExit("Ajoute d'abord DATABASE_URL (Supabase) dans ton fichier .env.")
    init_db()
    local = sqlite3.connect(DB_PATH)
    local.row_factory = sqlite3.Row
    cards = local.execute("SELECT * FROM cards ORDER BY id").fetchall()
    has_audio_table = local.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audio'").fetchone()
    audios = {r["name"]: r["data"] for r in local.execute("SELECT * FROM audio")} if has_audio_table else {}
    if os.path.isdir(AUDIO_DIR):  # mp3 encore dans le dossier audio/
        for name in os.listdir(AUDIO_DIR):
            if name.endswith(".mp3") and name not in audios:
                with open(os.path.join(AUDIO_DIR, name), "rb") as f:
                    audios[name] = f.read()
    # On garde les mêmes numéros de carte et noms de mp3 : Anki reconnaîtra les cartes déjà importées.
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
                                      audio_word, audio_sentence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["id"], r["word"], r["context"], r["sentence_fr"], r["word_en"], r["sentence_en"],
                 r["status"], r["error"], r.get("audio_word"), r.get("audio_sentence")))
            for name in (r.get("audio_word"), r.get("audio_sentence")):
                if name and name in audios:
                    conn.execute("INSERT INTO audio (name, data) VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
                                 (name, audios[name]))
            copied += 1
        conn.execute("SELECT setval(pg_get_serial_sequence('cards', 'id'), (SELECT COALESCE(MAX(id), 1) FROM cards))")
    if conflicts:
        print("⚠️  Non copiés (la base en ligne a déjà une autre carte au même numéro) :", ", ".join(conflicts))
    print(f"{copied} carte(s) copiée(s) vers la base en ligne.")


# ---------- Groq ----------

PROMPT = """Tu aides une anglophone à apprendre le français.
Mot ou expression en français : "{word}"
{context_line}
Réponds UNIQUEMENT avec un objet JSON contenant :
- "word_en" : la traduction anglaise du mot (le sens le plus courant, ou celui du contexte donné)
- "sentence_fr" : une phrase d'exemple naturelle en français (niveau B1, 8 à 15 mots) qui utilise ce mot et en montre clairement le sens
- "sentence_en" : la traduction anglaise complète de cette phrase"""


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
            "User-Agent": "french-anki-app/1.0",
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


def generate(word, context=""):
    global _model
    if not GROQ_API_KEY:
        raise RuntimeError("La variable d'environnement GROQ_API_KEY n'est pas définie.")
    context_line = f"Contexte ou sens voulu : {context}" if context else ""
    body = {
        "model": _model or GROQ_MODEL,
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": PROMPT.format(word=word, context_line=context_line)}],
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
    for key in ("word_en", "sentence_fr", "sentence_en"):
        if not str(result.get(key, "")).strip():
            raise RuntimeError(f"Réponse Groq incomplète, champ manquant : {key}")
    return result


def add_word(word, context="", tally_response_id=None):
    """Enregistre le mot, appelle Groq, stocke le résultat. Renvoie l'id de la carte."""
    word = word.strip()
    context = (context or "").strip()
    with db() as conn:
        try:
            card_id = conn.execute(
                "INSERT INTO cards (word, context, tally_response_id) VALUES (?, ?, ?) RETURNING id",
                (word, context, tally_response_id),
            ).fetchone()["id"]
        except Exception as e:
            if is_duplicate(e):
                return None  # Tally a renvoyé la même soumission : on l'ignore.
            raise
    process_card(card_id)
    return card_id


def process_card(card_id):
    """Génère ce qui manque pour la carte : d'abord le texte (Groq), puis l'audio (ElevenLabs)."""
    with db() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    try:
        if not row["sentence_fr"]:
            r = generate(row["word"], row["context"])
            with db() as conn:
                conn.execute(
                    "UPDATE cards SET sentence_fr=?, word_en=?, sentence_en=? WHERE id=?",
                    (r["sentence_fr"].strip(), r["word_en"].strip(), r["sentence_en"].strip(), card_id),
                )
                row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if ELEVENLABS_API_KEY and not (row["audio_word"] and row["audio_sentence"]):
            audio_word = save_audio(row["word"], f"motsfr_{card_id}_mot.mp3")
            audio_sentence = save_audio(row["sentence_fr"], f"motsfr_{card_id}_phrase.mp3")
            with db() as conn:
                conn.execute("UPDATE cards SET audio_word=?, audio_sentence=? WHERE id=?",
                             (audio_word, audio_sentence, card_id))
        with db() as conn:
            conn.execute("UPDATE cards SET status='done', error=NULL WHERE id=?", (card_id,))
    except Exception as e:  # on garde le mot et on note l'erreur pour réessayer plus tard
        with db() as conn:
            conn.execute("UPDATE cards SET status='error', error=? WHERE id=?", (str(e), card_id))


# ---------- ElevenLabs ----------

_voice_id = None


def elevenlabs_request(path, body=None):
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json",
                 "User-Agent": "french-anki-app/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ElevenLabs a répondu {e.code} : {e.read().decode(errors='replace')[:300]}")


def voice_id():
    """ELEVENLABS_VOICE_ID si définie, sinon une voix française de ton compte (ou la première)."""
    global _voice_id
    if ELEVENLABS_VOICE_ID:
        return ELEVENLABS_VOICE_ID
    if not _voice_id:
        voices = json.loads(elevenlabs_request("/voices")).get("voices", [])
        if not voices:
            raise RuntimeError("Aucune voix trouvée sur ton compte ElevenLabs.")
        def is_french(v):
            text = (v.get("name", "") + " " + json.dumps(v.get("labels") or {})).lower()
            return "french" in text or "fran" in text
        _voice_id = next((v for v in voices if is_french(v)), voices[0])["voice_id"]
    return _voice_id


def save_audio(text, filename):
    """Lit le texte en français avec ElevenLabs et enregistre le mp3. Renvoie le nom du fichier."""
    audio = elevenlabs_request(
        f"/text-to-speech/{voice_id()}?output_format=mp3_44100_128",
        {"text": text, "model_id": ELEVENLABS_MODEL},
    )
    put_audio(filename, audio)
    return filename


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
    """Renvoie (liste de (mot, contexte), id de la réponse)."""
    data = payload.get("data", {})
    fields = data.get("fields", [])
    word, context = "", ""
    for f in fields:
        label = (f.get("label") or "").strip().lower()
        if label in WORD_LABELS and not word:
            word = field_text(f)
        elif label in CONTEXT_LABELS and not context:
            context = field_text(f)
    if not word:  # sinon : premier champ texte
        for f in fields:
            if f.get("type") in ("INPUT_TEXT", "TEXTAREA") and f.get("value"):
                word = field_text(f)
                break
    return pair_words(word, context), data.get("responseId")


# ---------- Export Anki ----------

def done_cards():
    with db() as conn:
        return conn.execute("SELECT * FROM cards WHERE status='done' ORDER BY id").fetchall()


def sound(filename):
    return f"[sound:{filename}]" if filename else ""


def front_back(r):
    e = lambda t: html.escape(t, quote=False)
    front = (f"<b>{e(r['word'])}</b> {sound(r['audio_word'])}<br><br>"
             f"<i>{e(r['sentence_fr'])}</i> {sound(r['audio_sentence'])}")
    back = f"<b>{e(r['word_en'])}</b><br><br><i>{e(r['sentence_en'])}</i>"
    return front, back


def export_csv():
    out = io.StringIO()
    # En-têtes reconnus par Anki (2.1.54+) : séparateur, HTML activé, noms de colonnes.
    out.write("#separator:Comma\n#html:true\n#columns:Front,Back,Tags\n#tags column:3\n")
    w = csv.writer(out)
    for r in done_cards():
        front, back = front_back(r)
        w.writerow([front, back, "francais"])
    return out.getvalue()


def export_audio_zip():
    """Les mp3, à copier dans le dossier collection.media d'Anki si on importe le CSV."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for r in done_cards():
            for name in (r["audio_word"], r["audio_sentence"]):
                data = get_audio(name) if name else None
                if data:
                    z.writestr(name, data)
    return buf.getvalue()


def export_apkg():
    """Paquet Anki complet (cartes + audio) : un double-clic suffit. Nécessite `pip install genanki`."""
    import genanki  # importé ici pour que le reste de l'app marche sans
    model = genanki.Model(
        1607392319, "Mots français (audio)",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[{"name": "Carte 1", "qfmt": "{{Front}}",
                    "afmt": "{{FrontSide}}<hr id=answer>{{Back}}"}],
        css=".card{font-family:arial;font-size:22px;text-align:center;color:black;background:white}",
    )
    deck = genanki.Deck(2059400110, "Français")
    media = []
    tmpdir = tempfile.mkdtemp()  # genanki lit les sons depuis des fichiers
    for r in done_cards():
        front, back = front_back(r)
        # guid stable : réimporter le paquet met à jour les cartes au lieu de les dupliquer
        deck.add_note(genanki.Note(model=model, fields=[front, back], tags=["francais"],
                                   guid=genanki.guid_for("motsfr", r["id"])))
        for name in (r["audio_word"], r["audio_sentence"]):
            data = get_audio(name) if name else None
            if data:
                with open(os.path.join(tmpdir, name), "wb") as f:
                    f.write(data)
                media.append(os.path.join(tmpdir, name))
    package = genanki.Package(deck)
    package.media_files = media
    path = os.path.join(tmpdir, "francais.apkg")
    try:
        package.write_to_file(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- Serveur web ----------

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mes mots français</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top}}
.err{{color:#b00}} a.btn,button{{background:#2a5bd7;color:#fff;border:0;padding:.5rem 1rem;border-radius:6px;text-decoration:none;cursor:pointer}}
input{{padding:.45rem;border:1px solid #bbb;border-radius:6px}} form{{margin:1rem 0}}
</style></head><body>
<h1>Mes mots français</h1>
<p>{count} carte(s) prête(s).
<a class="btn" href="/export.apkg">Télécharger le paquet Anki (avec audio)</a>
<a href="/export.csv">CSV</a> · <a href="/audio.zip">fichiers audio (.zip)</a></p>
<form method="post" action="/add">
<input name="word" placeholder="mot en français" required>
<input name="context" placeholder="contexte (optionnel)">
<button>Ajouter</button></form>
<form method="post" action="/retry"><button>Réessayer les erreurs / ajouter l'audio manquant</button></form>
<table><tr><th>Mot</th><th>Phrase</th><th>English</th></tr>{rows}</table>
</body></html>"""


def render_home():
    with db() as conn:
        rows = conn.execute("SELECT * FROM cards ORDER BY id DESC").fetchall()
    e = html.escape
    trs = []
    for r in rows:
        if r["status"] == "done":
            audio = "".join(f'<br><audio controls preload="none" src="/audio/{e(n)}"></audio>'
                            for n in (r["audio_word"], r["audio_sentence"]) if n)
            trs.append(f"<tr><td><b>{e(r['word'])}</b></td><td>{e(r['sentence_fr'])}{audio}</td>"
                       f"<td><b>{e(r['word_en'])}</b><br>{e(r['sentence_en'])}</td></tr>")
        else:
            msg = e(r["error"] or "en cours…")
            trs.append(f"<tr><td><b>{e(r['word'])}</b></td><td colspan=2 class=err>{msg}</td></tr>")
    count = sum(1 for r in rows if r["status"] == "done")
    return PAGE.format(count=count, rows="".join(trs))


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
        path = urllib.parse.urlparse(self.path).path
        if not self.authorized():
            return
        if path == "/":
            self.send(200, render_home())
        elif path == "/export.csv":
            self.send(200, export_csv().encode("utf-8"), "text/csv; charset=utf-8",
                      {"Content-Disposition": 'attachment; filename="anki_francais.csv"'})
        elif path == "/export.apkg":
            try:
                data = export_apkg()
            except ImportError:
                return self.send(500, "Pour le paquet Anki, installe genanki : python -m pip install genanki",
                                 "text/plain; charset=utf-8")
            self.send(200, data, "application/octet-stream",
                      {"Content-Disposition": 'attachment; filename="francais.apkg"'})
        elif path == "/audio.zip":
            self.send(200, export_audio_zip(), "application/zip",
                      {"Content-Disposition": 'attachment; filename="audio_francais.zip"'})
        elif path.startswith("/audio/"):
            name = os.path.basename(urllib.parse.unquote(path))
            data = get_audio(name) if name.endswith(".mp3") else None
            if not data:
                return self.send(404, "Page introuvable")
            self.send(200, data, "audio/mpeg")
        else:
            self.send(404, "Page introuvable")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        raw = self.read_body()
        if path != "/webhook/tally" and not self.authorized():
            return
        if path == "/webhook/tally":
            if not verify_tally_signature(raw, self.headers.get("Tally-Signature")):
                return self.send(401, "Signature invalide", "text/plain")
            try:
                pairs, response_id = parse_tally(json.loads(raw))
            except (ValueError, AttributeError):
                return self.send(400, "JSON invalide", "text/plain")
            for i, (w, ctx) in enumerate(pairs):
                rid = f"{response_id}:{i}" if response_id else None
                add_word(w, ctx, rid)
            self.send(200, json.dumps({"ok": True, "words": [w for w, _ in pairs]}), "application/json")
        elif path == "/add":
            form = urllib.parse.parse_qs(raw.decode())
            word = form.get("word", [""])[0]
            if word.strip():
                for w, ctx in pair_words(word, form.get("context", [""])[0]):
                    add_word(w, ctx)
            self.redirect()
        elif path == "/retry":
            with db() as conn:
                query = "SELECT id FROM cards WHERE status='error'"
                if ELEVENLABS_API_KEY:
                    query += " OR audio_word IS NULL OR audio_sentence IS NULL"
                ids = [r["id"] for r in conn.execute(query)]
            for card_id in ids:
                process_card(card_id)
            self.redirect()
        else:
            self.send(404, "Page introuvable")


if __name__ == "__main__":
    if sys.argv[1:] == ["copier"]:
        copy_local_to_online()
        raise SystemExit
    init_db()
    if not GROQ_API_KEY:
        print("⚠️  GROQ_API_KEY n'est pas définie : les phrases ne pourront pas être générées.")
    if not ELEVENLABS_API_KEY:
        print("ℹ️  ELEVENLABS_API_KEY n'est pas définie : les cartes seront créées sans audio.")
    print("Base :", "Supabase (en ligne)" if DATABASE_URL else DB_PATH)
    print(f"App lancée sur http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
