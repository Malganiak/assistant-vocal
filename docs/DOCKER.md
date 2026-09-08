# Docker : ce qui tourne où, et pourquoi

## Pourquoi ce n'est pas 100 % conteneurisé sur un Mac

C'est la question la plus légitime du projet, et la réponse est une contrainte matérielle,
pas un raccourci.

Docker Desktop sur macOS fait tourner une **VM Linux**, et cette VM n'a **aucun accès à
Metal**, le GPU d'Apple. Or le moteur de synthèse choisit son implémentation en dur, sur le
nom de la plateforme :

```python
# speech_to_speech/TTS/qwen3_tts_handler.py
self.backend = "mlx" if platform == "darwin" else "faster_qwen3_tts"
```

Dans un conteneur, `platform` vaut `linux`. Le moteur basculerait donc sur son chemin CPU.
Pour donner l'échelle, mesuré sur ce Mac : Qwen3-TTS 1.7B en 8 bits **sur Metal** génère
2,8 fois plus vite que le temps réel ; le même modèle en bf16 **toujours sur Metal** tombe
déjà à 0,69, c'est-à-dire plus lent que la parole. Le processeur d'une VM est un ordre de
grandeur en dessous de tout ça.

Ce n'est pas un réglage à trouver : il n'existe aucun moyen d'exposer Metal à un conteneur
Linux. Le dépôt amont ne s'y trompe pas — son `docker-compose.yml` est exclusivement NVIDIA,
et même son `Dockerfile.arm64` garde une base CUDA : il cible les cartes Jetson, pas les Mac.

D'où la répartition retenue :

| Plateforme | Moteur d'inférence | UI de démo |
|---|---|---|
| macOS Apple Silicon | **natif**, via `uv` (MLX/Metal) | conteneur |
| Linux + NVIDIA | conteneur CUDA | conteneur |
| Windows + WSL2 + NVIDIA | conteneur CUDA | conteneur |
| Windows ou Linux sans NVIDIA | aucun chemin temps réel | conteneur |

`assistant-vocal up` masque cette différence : une seule commande, sur les trois systèmes.

## Les deux profils

```bash
# macOS : moteur natif + UI en conteneur
COMPOSE_PROFILES=ui docker compose up -d

# Linux/NVIDIA : les deux en conteneur
COMPOSE_PROFILES=gpu docker compose up -d
```

En pratique, on ne tape jamais ces commandes : `assistant-vocal up` choisit le profil, puis
**affiche la commande docker exacte** qu'il exécute — de quoi la rejouer à la main quand
quelque chose cloche.

## Pourquoi l'image de l'UI n'utilise pas le Dockerfile amont

Le `demo/Dockerfile` du dépôt amont a une première étape `node:24-slim` qui lance
`npm ci --omit=dev` pour n'en extraire **qu'un seul fichier** : le paquet navigateur de
`@openai/agents-realtime`, que `index.html` charge par
`<script src="vendor/openai-realtime-agents.umd.js">`.

Ce `npm ci` ne passe pas depuis l'extérieur de Hugging Face. Leur `package-lock.json` résout
ses 106 paquets depuis `https://npm.registries.huggingface.tech/`, leur registre interne, et
leur `.npmrc` ajoute `min-release-age=7`. Constaté sur cette machine : la commande reste
bloquée plus de vingt minutes, sans un message.

Ce projet remplace donc cette étape par le téléchargement du même fichier depuis un CDN
public, **à la version exacte que leur verrou épingle** (0.14.3). Vérifié : le fichier servi
par jsdelivr est identique à l'octet près à celui que produit `npm ci` — 4 812 462 octets.
Le reste de l'image est identique à l'amont. Bénéfice secondaire : plus d'étape node du tout,
donc une construction en moins d'une minute au lieu de dix.

C'est écrit en `dockerfile_inline` dans `compose.yaml`, à côté du contexte git : on garde donc
les sources de l'UI épinglées au tag amont, sans copier un seul fichier dans ce dépôt.

## Le piège des profils : `docker compose down` ne suffit pas

Les deux services sont derrière un profil. Un `docker compose down` **nu ne les arrête pas** :
Compose ne cible pas les services filtrés par profil. Constaté ici — le conteneur `ui`
survivait à un `down`, gardait le port 7860, et le lancement suivant échouait sur
« le port 7860 est deja pris ».

Utilisez donc toujours :

```bash
uv run assistant-vocal down
```

qui passe `COMPOSE_PROFILES=*`, ce qui sélectionne tous les services. À la main, l'équivalent
est `COMPOSE_PROFILES='*' docker compose down --remove-orphans`.

## Limites connues

Chaque entrée donne d'abord le **symptôme observable**, puis la cause.

### Le daemon Docker est arrêté

**Symptôme** — `dial unix /Users/.../docker.sock: connect: no such file or directory`.

**Le piège** — `docker --version` répond parfaitement : c'est le *client*. Le moteur qui
exécute les conteneurs est un service séparé.

**Ce que fait le projet** — `up` sonde le daemon avant tout le reste et affiche un
paragraphe en français avec le geste pour les trois systèmes, jamais une trace Python. Il
réessaie pendant 60 secondes, parce que sur macOS une commande docker réveille parfois
Docker Desktop toute seule.

### Un seul transport à la fois : WebSocket **ou** WebRTC

**Symptôme** — En basculant Réglages → Transport sur WebRTC dans l'UI : « handshake failed ».

**Cause** — La variable `SPEECH_TO_SPEECH_URL` sert deux usages contradictoires. En
**WebSocket**, c'est le **navigateur** qui ouvre la connexion ; il tourne sur l'hôte, donc il
lui faut `ws://localhost:8765`. En **WebRTC**, c'est le **serveur de démo**, depuis le
conteneur, donc il lui faut `ws://host.docker.internal:8765`. Une seule valeur ne peut
satisfaire qu'un transport.

**Ce que fait le projet** — WebSocket par défaut. C'est déjà le défaut de l'UI, il a plus de
fonctionnalités (relecture des enregistrements, réduction de bruit), et surtout la même URL
fonctionne dans les deux profils. Pour essayer le WebRTC, sur macOS uniquement :

```bash
uv sync --extra webrtc          # aiortc n'est pas installe par defaut
VOICE_S2S_URL=ws://host.docker.internal:8765/v1/realtime uv run assistant-vocal up
```

Le WebSocket cesse alors de fonctionner. **Et en profil GPU, le WebRTC est carrément
impossible** : l'image ne contient pas `aiortc`, donc `POST /v1/realtime/calls` répond 501.

### Le micro exige `localhost` ou HTTPS

**Symptôme** — En ouvrant `http://192.168.1.42:7860` depuis un téléphone, la page s'affiche
mais cliquer ne fait rien ; la console du navigateur signale `getUserMedia` indisponible.

**Cause** — Les navigateurs réservent l'accès au micro aux origines sûres. `localhost` et
`127.0.0.1` en sont, une adresse de réseau local en clair non.

**Ce que fait le projet** — Les deux ports sont publiés sur `127.0.0.1` uniquement. L'erreur
devient impossible, et l'API temps réel — qui n'a **aucune authentification** — n'est pas
exposée au réseau local par la même occasion.

### `host.docker.internal` n'existe pas sous Linux

**Cause** — C'est un nom fourni par Docker Desktop. Sous Linux il n'existe pas.

**Ce que fait le projet** — `extra_hosts: ["host.docker.internal:host-gateway"]` sur le
service `ui` crée le nom sous Linux et ne gêne pas Docker Desktop. Cela ne sert de toute
façon qu'au transport WebRTC.

### Le premier build est long

**Symptôme** — `up --rebuild` semble bloqué plusieurs minutes sans progression apparente.

**Ordres de grandeur** — L'image de l'UI prend une dizaine de minutes (clone du dépôt amont,
`npm ci` sur une centaine de paquets, puis `pip install`). L'image CUDA du moteur, 10 à
25 minutes (base CUDA d'environ 3 Go, puis torch et transformers).

**À savoir** — Les lancements suivants ne reconstruisent rien. En revanche, un
`up --rebuild` a besoin du réseau **même quand tout est en cache**, parce que BuildKit doit
re-résoudre la référence git de l'UI.

### Le cache des poids pèse lourd

**Symptôme** — En profil GPU, le premier démarrage affiche « Le moteur d'inférence démarre
toujours » pendant très longtemps.

**Cause** — Le port n'ouvre qu'**après** le chargement des modèles : les handlers chargent
leurs poids dans leur constructeur, et le pipeline est bâti avant que le serveur HTTP
n'écoute. « Sain » signifie donc « pipeline prêt », d'où le `start_period: 1800s`.

**Ce que fait le projet** — Le cache Hugging Face de l'hôte est monté dans le conteneur, donc
un `down` ne fait rien retélécharger. Sur ce Mac, les 7 Go nécessaires sont déjà présents :
`assistant-vocal doctor` le confirme dépôt par dépôt.

### Sous Linux, les fichiers du cache appartiennent à root

**Symptôme** — Après un run en profil GPU, un `huggingface-cli download` lancé en natif
échoue avec `PermissionError`.

**Cause** — Le conteneur tourne en root, et un montage lié ne remappe pas les identifiants
d'utilisateur sous Linux.

**Correctif** :

```bash
sudo chown -R "$USER":"$USER" ~/.cache/huggingface
```

Ce problème n'existe ni sur macOS ni pour le profil `ui`.

### Windows sans GPU NVIDIA n'a aucun chemin temps réel

Le point d'entrée y est portable (du Python, aucun `make`, aucun shell POSIX) et l'UI en
conteneur y tourne à l'identique. C'est la *performance* du moteur qui ne suit pas :
`assistant-vocal up` s'arrête donc avec un message explicite, et `--mode natif` reste
possible pour qui veut vérifier le câblage en connaissance de cause.

### Le tag amont pourrait bouger

L'UI est construite depuis `github.com/huggingface/speech-to-speech.git#v1.0.0:demo`. Un tag
git peut être déplacé de force par le dépôt amont. Le SHA attendu est noté en commentaire
dans `compose.yaml` :

```
tag v1.0.0 -> objet de tag 3914b2cda09a4ac59ecdf6f3324f2a06fe1bd896
           -> commit       16d7f98ff712fb082d53497937f5456667c84680
```

Pour vérifier soi-même :

```bash
git ls-remote --tags https://github.com/huggingface/speech-to-speech.git v1.0.0
```

Attention : `v1.0.0` est un tag **annoté**, donc `ls-remote` renvoie le SHA de l'objet de tag
(le premier), pas celui du commit.

## Modifier l'UI de démo

Par défaut, aucun fichier de l'UI n'est copié dans ce dépôt : Docker la construit directement
depuis le dépôt amont. C'est ce qui évite un sous-module, un script de synchronisation et un
mélange de licences dans le même arbre.

Pour la patcher, récupérez-en une copie et basculez le contexte de build avec un
`compose.override.yaml` — Compose le charge automatiquement, et il est ignoré par git :

```bash
git clone --depth 1 --filter=blob:none --sparse \
    https://github.com/huggingface/speech-to-speech.git vendor/s2s
cd vendor/s2s && git sparse-checkout set demo && git checkout v1.0.0
```

```yaml
# compose.override.yaml
services:
  ui:
    build:
      context: ./vendor/s2s/demo
```

## Ce qui reste à valider sur une machine NVIDIA

Le profil `gpu` est écrit d'après la documentation amont et son `docker-compose.yml`. Il n'a
**jamais été exécuté** : ce projet a été développé sur un Mac. Liste des contrôles à faire,
dans l'ordre :

1. `docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi` — le NVIDIA
   Container Toolkit est-il installé et la réservation honorée ?
2. `nvidia-smi` — le pilote annonce-t-il CUDA 12.8 ou plus récent ?
3. Le build aboutit : `COMPOSE_PROFILES=gpu docker compose build backend`.
4. Assez de VRAM pour Parakeet, Qwen3-4B et Qwen3-TTS 1.7B en même temps. Sinon, passer
   `VOICE_LLM_MODE=api` pour sortir le modèle de langue du GPU.
5. Le débit réel de la synthèse sur ce GPU. Les chiffres cités dans ce dépôt sont des mesures
   Metal, ils ne s'y transposent pas.
6. `docker compose ps` doit finir par afficher `healthy` pour `backend`, pas `unhealthy`.
7. La propriété des fichiers du cache, voir plus haut.

Une différence connue et importante : sur Linux, le moteur de synthèse est
`faster-qwen3-tts`, pas MLX. **La rustine 2 (paramètres d'échantillonnage) n'y a donc aucun
effet** : la méthode de streaming de ce moteur a une liste d'arguments fermée qui n'inclut pas
`gen_kwargs`. Les émotions, elles, fonctionnent — `instruct` y est bien accepté.
