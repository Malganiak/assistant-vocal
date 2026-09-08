# Raccourcis de confort, sous macOS et Linux.
#
# SOUS WINDOWS, ignorez ce fichier : `make` n'y est pas installe par defaut.
# Toutes les cibles ci-dessous ne sont que des alias de `uv run assistant-vocal`,
# qui fonctionne a l'identique sur les trois plateformes. Le Makefile n'est
# jamais l'implementation, seulement un raccourci.

RUN := uv run

.DEFAULT_GOAL := help
.PHONY: help setup up down run serve doctor prompt test lint format check clean

help: ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Cree l'environnement et installe les dependances
	uv sync
	@echo "OK. Etape suivante : make doctor"

up: ## Tout demarrer : moteur + UI dans le navigateur
	$(RUN) assistant-vocal up

down: ## Arreter ce qui traine encore
	$(RUN) assistant-vocal down

build: ## Reconstruire les images docker
	$(RUN) assistant-vocal up --rebuild

run: ## Parler au micro de la machine, sans Docker ni navigateur
	$(RUN) assistant-vocal run

serve: ## Lancer le moteur seul (l'UI se lance a part)
	$(RUN) assistant-vocal serve

doctor: ## Verifier l'installation, sans rien charger
	$(RUN) assistant-vocal doctor

prompt: ## Copier le prompt systeme dans le presse-papier
	$(RUN) assistant-vocal prompt --copy

test: ## Tests unitaires (aucun modele, aucun micro)
	$(RUN) pytest

lint: ## Lint et verification du formatage
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format: ## Reformater le code
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

compose-check: ## Valider compose.yaml sur les deux profils (sans daemon)
	COMPOSE_PROFILES=ui  docker compose config -q
	COMPOSE_PROFILES=gpu docker compose config -q
	@echo "compose.yaml valide sur les deux profils."

check: lint test compose-check ## Tout verifier

clean: ## Supprimer les caches d'outillage
	rm -rf .ruff_cache .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
