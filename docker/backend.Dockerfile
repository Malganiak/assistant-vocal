# Moteur d'inference pour Linux ou WSL2 avec un GPU NVIDIA.
#
# Cette image n'a AUCUN interet sur un Mac : Docker Desktop y fait tourner une
# VM Linux sans acces a Metal, donc la synthese basculerait sur le chemin CPU,
# beaucoup trop lent pour une conversation. Sur macOS, le moteur tourne en
# natif via uv -- voir docs/DOCKER.md.
#
# Les choix repris du Dockerfile amont, parce qu'ils sont justes :
#   - la base CUDA 12.8.1 sur Ubuntu 24.04. Ce n'est pas interchangeable : la
#     roue de qwentts-cpp-python, tiree par faster-qwen3-tts[ggml], est publiee
#     en manylinux_2_39, c'est-a-dire glibc 2.39, c'est-a-dire Ubuntu 24.04 ;
#   - libsndfile1 et libportaudio2, dont dependent les bibliotheques audio ;
#   - le pre-telechargement des donnees nltk, qu'il vaut mieux avoir dans
#     l'image que decouvrir au premier tour de parole.
#
# Ce qu'on fait differemment : on installe NOTRE projet avec NOTRE uv.lock. Le
# depot amont n'embarque pas de lock, donc son `uv sync` resout les dependances
# a la date de la construction -- deux images construites a un mois d'ecart ne
# sont pas les memes. Avec un lock, elles le sont.

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libportaudio2 \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# uv fournit lui-meme Python 3.13 : la base Ubuntu 24.04 n'a que 3.12, et le
# projet demande 3.13. Un binaire copie depuis l'image officielle, pas de
# script d'installation a executer.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

# Deux `uv sync` plutot qu'un seul, pour que la couche des dependances (lourde :
# torch, transformers) soit mise en cache independamment de notre code. Editer
# src/ ne fait alors reconstruire que la derniere couche.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-dev

COPY src ./src
RUN uv sync --locked --no-dev

# `punkt_tab` sert au decoupage en phrases du texte du LLM, et
# `averaged_perceptron_tagger_eng` a la phonetisation.
RUN python -c "import nltk; nltk.download('punkt_tab'); nltk.download('averaged_perceptron_tagger_eng')"

EXPOSE 8765

# La configuration vient du .env, injecte par compose. VOICE_HOST y est forcé
# a 0.0.0.0 : dans un conteneur, ecouter sur la boucle locale rendrait le
# service injoignable.
CMD ["assistant-vocal", "serve"]
