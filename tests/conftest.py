"""Isole les tests de l'environnement de la machine.

Sans cette precaution, un `VOICE_TTS_SPEAKER=serena` exporte dans le terminal,
ou le `.env` du depot, changeraient silencieusement le resultat des tests. Les
tests deviendraient alors « verts chez moi, rouges chez toi ».
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def environnement_neutre(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Retire les variables VOICE_* et se place hors de portee du .env."""
    # On copie les cles avant de les supprimer : on ne modifie pas un
    # dictionnaire pendant qu'on l'itere.
    for nom in [cle for cle in os.environ if cle.startswith("VOICE_")]:
        monkeypatch.delenv(nom, raising=False)

    # `Settings` lit `.env` relativement au repertoire courant. En se placant
    # dans un dossier temporaire, il n'y a plus de .env a trouver.
    monkeypatch.chdir(tmp_path)
