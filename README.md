# assistant-vocal

Assistant vocal francophone à voix expressive, qui tourne **en local** sur Apple Silicon.

Ce dépôt **ne réimplémente aucun pipeline vocal**. Il configure et lance
[`speech-to-speech`](https://github.com/huggingface/speech-to-speech) 1.0.0 de Hugging Face,
et y ajoute deux choses qu'elle ne sait pas faire :

- **les émotions pilotées par le modèle de langue** — il préfixe chaque réplique d'une
  étiquette (`[joie]`, `[empathie]`…) que le projet traduit en consigne de ton pour la
  synthèse ;
- **le réglage de l'échantillonnage de la synthèse**, qui n'a aucune option en ligne de
  commande dans la bibliothèque.

Le dépôt fait environ 1 300 lignes de Python largement commenté, plus 650 lignes de tests
(60 tests, moins d'une seconde, aucun modèle chargé). Tout le reste — détection de parole,
transcription, modèle de langue, synthèse, protocole temps réel — vient de la bibliothèque.

## La pile

| Rôle | Modèle | Taille |
|---|---|---|
| Détection de parole | Silero VAD + Smart Turn v3 | 8 Mo |
| Transcription | `mlx-community/parakeet-tdt-0.6b-v3` | 2,3 Go |
| Modèle de langue | `mlx-community/Qwen3-4B-Instruct-2507-4bit` | 2,1 Go |
| Synthèse | `mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit` | 2,5 Go |

C'est exactement ce que sélectionne le préréglage `--mac-optimal-settings` de la
bibliothèque. Rien ne sort de la machine.

## Prérequis

| | |
|---|---|
| Machine | Mac Apple Silicon (M1 ou plus récent), 16 Go de mémoire au minimum |
| Outils | [`uv`](https://docs.astral.sh/uv/) — `brew install uv` |
| Pour l'UI web | Docker Desktop, démarré |
| Python | installé automatiquement par `uv` (3.13) |
| Disque | environ 7 Go de poids, plus 2 Go d'environnement |

Linux et Windows sont pris en charge avec un GPU NVIDIA — voir
[docs/DOCKER.md](docs/DOCKER.md), et lisez bien la section « ce qui reste à valider » : ce
chemin n'a pas été exécuté.

## Démarrage

```bash
uv sync
uv run assistant-vocal doctor
```

`doctor` ne charge aucun modèle et répond en moins d'une seconde. Il affiche les réglages
résolus, vérifie que les quatre dépôts de poids sont dans le cache, et montre la ligne
d'arguments exacte qui sera passée à la bibliothèque. **C'est la commande à lancer en premier
quand quelque chose ne marche pas.**

Ensuite, deux façons de parler :

```bash
# 1. Micro et haut-parleurs de la machine, sans Docker ni navigateur.
#    Le chemin le plus court pour verifier que tout fonctionne.
uv run assistant-vocal run

# 2. Dans le navigateur, avec l'UI de demo de Hugging Face.
uv run assistant-vocal up
#    puis ouvrir http://localhost:7860/
```

`up` démarre le moteur d'inférence **et** l'UI, fusionne leurs journaux dans le terminal, et
`Ctrl-C` arrête tout dans le bon ordre. Le premier lancement construit l'image de l'UI : une
dizaine de minutes, une seule fois.

| | `run` | `up` |
|---|---|---|
| Interface | terminal | navigateur |
| Docker | non | oui, pour l'UI |
| Choix de la voix | `.env` | menu déroulant de l'UI |
| Interruption à la voix | oui, si `VOICE_BLOCK_MIC_DURING_PLAYBACK=false` | oui |
| Historique de conversation affiché | non | oui |

## Configuration

Tout est facultatif : sans `.env`, le projet fonctionne tel quel.

```bash
cp .env.example .env
```

Les cinq réglages qu'on touche vraiment :

| Variable | Défaut | À quoi ça sert |
|---|---|---|
| `VOICE_TTS_SPEAKER` | `aiden` | La voix, parmi neuf. `aiden` est la plus grave, `serena` la plus aiguë |
| `VOICE_LLM_MODE` | `local` | `api` pour déléguer le modèle de langue à un service HTTP |
| `VOICE_TTS_TEMPERATURE` | `0.9` | Plus bas = plus stable et plus plat |
| `VOICE_BLOCK_MIC_DURING_PLAYBACK` | `true` | `false` au casque, pour pouvoir couper la parole |
| `VOICE_LOG_LEVEL` | `info` | `debug` affiche l'émotion choisie à chaque réplique |

[`.env.example`](.env.example) documente les seize, une par une.

### Déléguer le modèle de langue à une API

La transcription et la synthèse restent locales ; seul le texte transcrit part chez le
fournisseur.

```bash
VOICE_LLM_MODE=api
VOICE_API_MODEL=gpt-5.6-terra
VOICE_API_KEY=sk-...
# Pour un serveur compatible en local, par exemple llama.cpp :
#VOICE_API_BASE_URL=http://127.0.0.1:8080/v1
```

## Les émotions

Le modèle de langue commence chaque réplique par une étiquette prise dans cette liste :

```
[neutre] [calme] [chaleur] [joie] [enthousiasme] [amusement] [curiosite] [empathie] [desole]
```

Le projet la lit, la traduit en consigne de ton pour la synthèse, et la **retire du texte**
avant de le prononcer. Une réplique arrive souvent en plusieurs morceaux dont seul le premier
porte l'étiquette : le ton est donc mémorisé jusqu'au changement de tour de parole.

En `VOICE_LOG_LEVEL=info`, chaque réplique produit une ligne :

```
assistant_vocal.emotions - INFO - emotion 'joie' -> consigne 'Happy and bright.'
```

Deux choix qui méritent une explication :

**Les consignes sont en anglais** (« Happy and bright. ») alors que la voix parle français.
Le modèle répond aux deux langues, mais les formulations anglaises donnent un contraste
d'énergie plus net : mesuré, « Sad, slow and quiet. » descend à 0,044 d'énergie moyenne contre
0,069 pour son équivalent français.

**La palette exclut la tristesse et la colère.** Sur quatre tirages de la même réplique, le
neutre tient 5,2 s et « Sad, slow and quiet. » monte à 7,3 s — 40 % plus lent, avec un
écart-type de 2,1 s. Baisser la température n'y change rien : c'est le style demandé qui est
long. Ces émotions cassent le rythme d'une conversation.

Si vous voyez cet avertissement, allez lire la section « Limites » :

```
aucune etiquette d'emotion dans la reponse du LLM. Le prompt du projet est-il bien actif ?
```

Le prompt système vit dans un seul fichier,
[`src/assistant_vocal/prompt_fr.txt`](src/assistant_vocal/prompt_fr.txt). Pour le lire ou le
copier :

```bash
uv run assistant-vocal prompt
uv run assistant-vocal prompt --copy
```

## Ce que ce dépôt rustine dans la bibliothèque

Trois rustines, posées en mémoire au démarrage — aucun fichier du paquet n'est modifié.
[`src/assistant_vocal/patches.py`](src/assistant_vocal/patches.py) explique chacune en
détail : ce qu'elle fait, pourquoi la bibliothèque ne permet pas de faire autrement, et ce
qui la casserait.

| # | Quoi | Pourquoi |
|---|---|---|
| 1 | Remplace la classe du handler de synthèse | C'est le seul moyen d'y greffer les émotions et le filtre d'arguments |
| 2 | Injecte les paramètres d'échantillonnage | `temperature`, `top_k`, `top_p` et `repetition_penalty` n'ont aucune option en ligne de commande |
| 3 | Protège le prompt système du projet | L'UI de démo envoie **toujours** son propre prompt, ce qui écraserait le nôtre et ferait cesser les émotions |

**C'est le fichier à relire après chaque montée de version** de `speech-to-speech`. Deux
sentinelles disent si les rustines s'appliquent encore : la ligne
`synthese avec emotions active` au démarrage, et l'avertissement sur les étiquettes manquantes
à la première réplique. Deux tests (`test_patches.py`) vérifient les mécanismes eux-mêmes.

## Limites connues

**Pas de Metal dans un conteneur.** Sur un Mac, le moteur d'inférence tourne en natif et
seule l'UI est conteneurisée. Ce n'est pas un raccourci : Docker Desktop fait tourner une VM
Linux sans accès au GPU d'Apple, et le moteur de synthèse choisit son implémentation en dur
sur le nom de la plateforme. `up --mode gpu` refuse donc de démarrer sur macOS.
[docs/DOCKER.md](docs/DOCKER.md) détaille les chiffres.

**Le champ « Instructions » de l'UI est ignoré**, et une ligne de journal le dit à chaque
fois. Le prompt du projet fait autorité. Pour expérimenter depuis l'UI :
`VOICE_ALLOW_UI_PROMPT=true`, puis `assistant-vocal prompt --copy` pour coller le prompt du
projet dans le champ — sans quoi les émotions cessent d'arriver.

**L'étiquette apparaît dans la transcription affichée** (`[joie] Bonjour !`) alors que la voix
dit bien « Bonjour ! ». Le texte envoyé au navigateur vient du handler de langue, en amont du
nôtre. La nettoyer demanderait une quatrième rustine, plus fragile ; et l'étiquette visible
est un bon signal de débogage.

**Un seul transport à la fois.** WebSocket par défaut. Le WebRTC demande l'extra `webrtc` et
une autre valeur d'URL, ce qui casse alors le WebSocket. Voir
[docs/DOCKER.md](docs/DOCKER.md).

**Il n'y a pas de réglage de température pour le modèle de langue.** Sur le backend MLX, la
bibliothèque n'honore que le nombre maximal de tokens : ce serait un bouton qui ne fait rien.

**Le profil Linux/NVIDIA n'a jamais été exécuté.** Il est écrit d'après la documentation
amont. La liste des contrôles à faire est dans [docs/DOCKER.md](docs/DOCKER.md).

## Dépannage

| Symptôme | Cause probable | Geste |
|---|---|---|
| L'assistant reste muet, sans erreur visible | Le handler de synthèse avale ses exceptions | Chercher `Error during Qwen3-TTS generation` dans le journal : locuteur invalide, ou argument de trop |
| `Warmup generation failed` au démarrage | Même cause, découverte deux secondes plus tôt | Ne jamais l'ignorer : c'est l'avertisseur le plus fiable |
| Il répond en anglais | Le prompt du projet a été écrasé | `VOICE_ALLOW_UI_PROMPT` est-il à `true` ? |
| `aucune etiquette d'emotion` | Idem | Même chose |
| `Ignoring options for inactive backends` | Un drapeau mal nommé | `uv run assistant-vocal doctor` et `uv run pytest` |
| `it is recommended to use mlx-lm` | Faux positif en mode API | À ignorer : la transcription et la synthèse restent bien sur le GPU |
| Le port 8765 ou 7860 est pris | Une instance traîne | `uv run assistant-vocal down` — et non `docker compose down`, qui ne touche pas aux services derrière un profil |
| `dial unix ... docker.sock` | Le daemon Docker est arrêté | Ouvrir Docker Desktop, ou `open -a Docker` |

## Développement

```bash
make test     # 60 tests, aucun modele charge, moins d'une seconde
make lint     # ruff : lint et formatage
make check    # tout, plus la validation des deux profils compose
```

Sous Windows, `make` n'existe pas : utilisez directement `uv run pytest`, `uv run ruff …`.

```
src/assistant_vocal/
  config.py       reglages (.env) et fabrication de la ligne d'arguments  [pur]
  prompt_fr.txt   le prompt systeme, source unique
  emotions.py     lecture des etiquettes, memoire par tour de parole      [pur]
  tts_handler.py  sous-classe du handler de synthese
  patches.py      les trois rustines, et pourquoi
  launcher.py     up / down, sondes docker, supervision des processus
  cli.py          les commandes
```

Les modules marqués `[pur]` n'importent ni torch ni la bibliothèque : c'est ce qui permet à la
majorité des tests de tourner instantanément.

### Monter de version

```bash
uv lock --upgrade-package speech-to-speech
make test
```

Puis relire [`patches.py`](src/assistant_vocal/patches.py), et vérifier au démarrage que les
deux lignes sentinelles apparaissent toujours.

## Licence

MIT.
