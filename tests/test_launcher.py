"""Tests du lanceur : detection de plateforme, sonde Docker, attente.

Aucun conteneur n'est demarre : `subprocess.run` est bouchonne. Ces tests
doivent tourner en une fraction de seconde.
"""

from __future__ import annotations

import subprocess

import pytest

from assistant_vocal import AssistantError
from assistant_vocal.launcher import Mode, attendre_http, detecter_mode, exiger_docker

# ---------------------------------------------------------------------------
# Detection de la plateforme
# ---------------------------------------------------------------------------


def test_mac_apple_silicon_donne_le_mode_natif(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    assert detecter_mode() is Mode.NATIF


def test_mac_intel_est_refuse(monkeypatch: pytest.MonkeyPatch):
    """MLX n'existe que pour les puces Apple Silicon."""
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    with pytest.raises(AssistantError, match="Apple Silicon"):
        detecter_mode()


def test_mode_gpu_refuse_sur_mac(monkeypatch: pytest.MonkeyPatch):
    """Le message doit expliquer POURQUOI, pas seulement refuser."""
    monkeypatch.setattr("sys.platform", "darwin")
    with pytest.raises(AssistantError, match="Metal") as capture:
        detecter_mode("gpu")
    assert "assistant-vocal up" in str(capture.value)


def test_linux_sans_nvidia_est_refuse(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(AssistantError, match="nvidia-smi"):
        detecter_mode()


def test_linux_avec_nvidia_donne_le_mode_gpu(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/nvidia-smi")
    assert detecter_mode() is Mode.GPU


def test_mode_natif_forcable_sur_linux(monkeypatch: pytest.MonkeyPatch):
    """L'echappatoire pour qui veut essayer sans GPU, en connaissance de cause."""
    monkeypatch.setattr("sys.platform", "linux")
    assert detecter_mode("natif") is Mode.NATIF


# ---------------------------------------------------------------------------
# Sonde du daemon Docker
# ---------------------------------------------------------------------------


def _reponse(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_docker_pret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _reponse(0, stdout="29.7.2\n"))
    exiger_docker(attente_s=0.0)  # ne doit pas lever


def test_daemon_eteint(monkeypatch: pytest.MonkeyPatch):
    """Le cas du jour : le client repond, le serveur non."""
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: _reponse(1, stderr="dial unix /var/run/docker.sock: no such file"),
    )
    with pytest.raises(AssistantError) as capture:
        exiger_docker(attente_s=0.0)

    message = str(capture.value)
    # Le message doit donner le geste pour les trois plateformes...
    assert "Docker Desktop" in message
    assert "systemctl start docker" in message
    # ...et remonter le detail technique, pas seulement « ca ne marche pas ».
    assert "docker.sock" in message


def test_sortie_vide_compte_comme_un_echec(monkeypatch: pytest.MonkeyPatch):
    """Un code de retour nul avec une sortie vide n'est pas une reussite.

    C'est le piege du tube : `docker info | head` renvoie le code de `head`.
    """
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _reponse(0, stdout="  \n"))
    with pytest.raises(AssistantError):
        exiger_docker(attente_s=0.0)


def test_docker_absent_a_son_propre_message(monkeypatch: pytest.MonkeyPatch):
    """« docker n'est pas installe » et « le daemon dort » sont deux problemes."""

    def introuvable(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("subprocess.run", introuvable)
    with pytest.raises(AssistantError, match="introuvable"):
        exiger_docker(attente_s=0.0)


def test_docker_desktop_qui_se_reveille(monkeypatch: pytest.MonkeyPatch):
    """Sur macOS, une commande docker reveille parfois Docker Desktop.

    La sonde doit donc reessayer, pas abandonner au premier echec.
    """
    reponses = [_reponse(1, stderr="not running"), _reponse(0, stdout="29.7.2")]
    monkeypatch.setattr("subprocess.run", lambda *a, **k: reponses.pop(0))
    monkeypatch.setattr("time.sleep", lambda _: None)

    exiger_docker(attente_s=10.0)  # ne doit pas lever
    assert not reponses, "la sonde n'a pas reessaye"


# ---------------------------------------------------------------------------
# Attente de disponibilite
# ---------------------------------------------------------------------------


class _ProcMort:
    """Un processus deja termine."""

    returncode = 1

    def poll(self) -> int:
        return self.returncode


def test_mort_precoce_signalee_immediatement(monkeypatch: pytest.MonkeyPatch):
    """On ne doit pas attendre trente minutes un service deja mort.

    La vraie cause est dans les lignes de journal qui precedent : le message
    doit y renvoyer.
    """
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(AssistantError, match="s'est arrete avant d'etre pret") as capture:
        attendre_http(
            "http://127.0.0.1:1/v1/pool",
            libelle="Le moteur",
            delai_s=1800,
            proc=_ProcMort(),  # type: ignore[arg-type]
        )
    assert "backend  |" in str(capture.value)


def test_delai_depasse(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(AssistantError, match="n'a pas repondu"):
        attendre_http("http://127.0.0.1:1/rien", libelle="Le moteur", delai_s=0.0)
