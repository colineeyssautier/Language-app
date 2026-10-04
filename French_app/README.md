# Mes mots français : Tally → Groq → Anki

Tu écris un mot français dans un formulaire Tally. L'app demande à Groq une phrase d'exemple et les traductions anglaises, garde tout dans une petite base de données (`words.db`), et te donne un fichier CSV à importer dans Anki.

Carte Anki :
- **Recto** : le mot en français + une phrase d'exemple
- **Verso** : la traduction anglaise du mot + de la phrase

---

## 1. Créer le formulaire Tally

1. Va sur [tally.so](https://tally.so), connecte-toi, clique sur **Create form** puis **Start from scratch**.
2. Donne un titre, par exemple *Nouveau mot*.
3. Ajoute la première question : tape `/` puis choisis **Short answer**.
   - Intitulé de la question : **Mot** (exactement ce mot, l'app le cherche par son nom)
   - Coche **Required**.
   - Astuce : si tu veux envoyer plusieurs mots d'un coup, choisis plutôt **Long answer** et mets un mot par ligne.
   - Pour préciser le sens d'un mot, écris-le entre parenthèses juste après : `avocat (le fruit)`. Chaque mot a ainsi son propre contexte.
4. (Optionnel) Ajoute une deuxième question **Short answer** nommée **Contexte**, non obligatoire. Elle sert à préciser le sens voulu, par ex. pour *avocat* : « le fruit ». Avec plusieurs mots, elle ne s'applique que si elle a autant de lignes que de mots (une ligne par mot). Les parenthèses sont plus simples.
5. Clique sur **Publish**. Garde le lien du formulaire dans tes favoris (ou sur ton téléphone) : c'est là que tu ajouteras tes mots.

## 2. Récupérer une clé Groq

1. Va sur [console.groq.com/keys](https://console.groq.com/keys) et crée une clé (**Create API Key**).
2. Garde-la pour toi : ne la colle jamais dans un chat ni dans le code. Elle va dans une variable d'environnement (étape suivante).

## 3. Lancer l'app sur ton ordinateur

Il faut seulement Python 3 (déjà installé sur Mac ; sur Windows, voir python.org). Aucune autre installation.

Dans un terminal, dans le dossier de l'app :

```bash
# Mac / Linux
export GROQ_API_KEY="ta-clé-groq"
python3 app.py
```

```powershell
# Windows (PowerShell)
$env:GROQ_API_KEY="ta-clé-groq"
python app.py
```

**Plus simple : un fichier `.env`.** Crée un fichier nommé `.env` dans le même dossier que `app.py` (sur le modèle de `env.example`) et mets-y tes clés, une par ligne : `GROQ_API_KEY=...` et `ELEVENLABS_API_KEY=...`. L'app le lit à chaque démarrage, plus besoin de retaper les clés.

Ouvre ensuite <http://localhost:8000>. Tu peux déjà y ajouter un mot à la main pour tester, sans Tally.

## 4. Relier Tally à l'app (webhook)

Tally doit pouvoir joindre ton app depuis Internet. Sur ton ordinateur, le plus simple est un tunnel gratuit :

```bash
# Dans un deuxième terminal (installer d'abord : https://ngrok.com/download)
ngrok http 8000
```

ngrok affiche une adresse du type `https://abcd-1234.ngrok-free.app`.

Puis dans Tally :

1. Ouvre ton formulaire → onglet **Integrations** → **Webhooks** → **Connect**.
2. **Endpoint URL** : `https://abcd-1234.ngrok-free.app/webhook/tally` (ton adresse ngrok + `/webhook/tally`).
3. (Recommandé) Remplis **Signing secret** avec un mot de passe de ton choix, et lance l'app avec la même valeur :
   `export TALLY_SIGNING_SECRET="ton-secret"` avant `python3 app.py`. Ainsi, seul Tally peut ajouter des mots.
4. Clique sur **Connect**, puis remplis ton formulaire une fois : le mot apparaît sur <http://localhost:8000> avec sa phrase.

Note : l'adresse ngrok gratuite change à chaque redémarrage, il faut alors la remettre dans Tally. Pour que ça marche tout le temps sans ton ordinateur allumé, on peut héberger l'app en ligne (Render, Railway…) : demande-moi quand tu veux le faire.

## 5. La voix française (ElevenLabs)

1. Sur [elevenlabs.io](https://elevenlabs.io), ouvre ton profil puis **API Keys**, et crée une clé.
2. Avant de lancer l'app, définis-la comme la clé Groq :
   `$env:ELEVENLABS_API_KEY="ta-clé-elevenlabs"` (Windows) ou `export ELEVENLABS_API_KEY="..."` (Mac).
3. (Optionnel) Pour choisir la voix : dans ElevenLabs, ouvre **Voices**, choisis une voix française et copie son **Voice ID**. Ensuite, définis `ELEVENLABS_VOICE_ID`. Sans ça, l'app prend une voix française de ton compte s'il y en a une.
4. Installe une seule fois le petit module qui fabrique les paquets Anki : `python -m pip install genanki`

Chaque nouveau mot reçoit deux fichiers audio : le mot seul, et la phrase. Pour les mots déjà enregistrés, clique sur **Réessayer les erreurs / ajouter l'audio manquant**. Tu peux écouter l'audio directement sur la page de l'app.

## 6. Importer dans Anki

1. Sur <http://localhost:8000>, clique sur **Télécharger le paquet Anki (avec audio)** pour obtenir `francais.apkg`.
2. Double-clique sur le fichier, ou dans Anki : **Fichier → Importer**. Les cartes arrivent dans le paquet **Français**, avec leur audio.
3. Quand une carte s'affiche, Anki lit automatiquement le mot puis la phrase. Les petits boutons ▶ permettent de réécouter, et la touche **R** relit tout.

Tu peux réimporter le paquet quand tu veux : les cartes déjà présentes sont mises à jour, pas dupliquées.

Si tu préfères le CSV : il contient des balises `[sound:...]`. Il faut alors télécharger aussi **fichiers audio (.zip)** et copier les mp3 dans le dossier `collection.media` d'Anki. Sur Windows, ce dossier est `%APPDATA%\Anki2\<ton profil>\collection.media`.

## Réglages (variables d'environnement)

| Variable | Rôle | Par défaut |
|---|---|---|
| `GROQ_API_KEY` | clé Groq (obligatoire) | |
| `GROQ_MODEL` | modèle Groq (si indisponible, l'app en choisit un autre automatiquement) | `openai/gpt-oss-120b` |
| `TALLY_SIGNING_SECRET` | vérifie que les requêtes viennent de Tally | vide (pas de vérification) |
| `ELEVENLABS_API_KEY` | clé ElevenLabs (sans elle : pas d'audio) | |
| `ELEVENLABS_VOICE_ID` | voix à utiliser | une voix française de ton compte |
| `ELEVENLABS_MODEL` | modèle de voix | `eleven_multilingual_v2` |
| `PORT` | port de l'app | `8000` |
| `DB_PATH` | fichier de la base | `words.db` à côté de `app.py` |

Si Groq échoue (clé absente, quota…), le mot est quand même gardé en rouge sur la page d'accueil ; le bouton **Réessayer les mots en erreur** relance la génération.

## Mettre l'app en ligne (Railway)

1. Mets le dossier de l'app sur GitHub, dans un dépôt **privé** : `app.py`, `railway.json`, `requirements.txt`, `.gitignore`. Le fichier `words.db` et le dossier `audio` ne doivent pas y aller.
2. Sur [railway.com](https://railway.com), clique sur **New Project**, puis **Deploy from GitHub repo**, et choisis le dépôt.
3. Dans le service, onglet **Variables**, ajoute :
   - `GROQ_API_KEY` : ta clé Groq
   - `APP_PASSWORD` : un mot de passe pour tes pages. Le navigateur le demandera ; l'identifiant n'a pas d'importance.
   - `TALLY_SIGNING_SECRET` : le même secret que dans Tally
   - `DB_PATH` : `/data/words.db` (l'audio sera rangé dans `/data/audio`)
   - `ELEVENLABS_API_KEY` : ta clé ElevenLabs
4. Fais un clic droit sur le service, puis **Attach Volume**, et choisis le chemin de montage `/data`. Comme ça, tes mots sont gardés quand l'app redémarre.
5. Onglet **Settings**, puis **Networking**, puis **Generate Domain**. Tu obtiens une adresse fixe, du type `https://ton-app.up.railway.app`.
6. Dans Tally, remplace l'adresse ngrok par `https://ton-app.up.railway.app/webhook/tally`.
