"""Tests du lanceur : detection de plateforme, sonde Docker, attente.

Aucun conteneur n'est demarre : `subprocess.run` est bouchonne. Ces tests
doivent tourner en une fraction de seconde.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from assistant_vocal import AssistantError
from assistant_vocal.launcher import (
    Mode,
    _pid_enregistre,
    _signaler,
    arreter_pid,
    attendre_http,
    detecter_mode,
    down,
    est_notre_moteur,
    exiger_docker,
    exiger_ports_libres,
    liberer_port_moteur,
    processus_du_port,
)

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


# ---------------------------------------------------------------------------
# Recuperation d'un moteur orphelin
#
# Le bug que cette section verrouille : un `up` interrompu brutalement laissait
# un moteur vivant, le port pris, ET le fichier PID efface. `down` n'avait alors
# plus aucune prise -- alors que c'est precisement ce que le message d'erreur
# conseillait. Il fallait tuer le processus a la main, `up` apres `up`.
# ---------------------------------------------------------------------------


NOTRE_MOTEUR = "/depot/.venv/bin/python3 -u -m assistant_vocal.cli serve"


class _Sortie:
    """Le strict minimum d'un `CompletedProcess` pour ces tests."""

    returncode = 0

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


class _Horloge:
    """Une horloge qui avance d'une seconde a chaque lecture.

    Elle remplace `time.monotonic` pour que les boucles d'attente de
    `arreter_pid` se terminent instantanement, sans toucher a leur logique.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


@pytest.mark.parametrize(
    ("ligne", "attendu"),
    [
        (NOTRE_MOTEUR, True),
        ("python -m assistant_vocal.cli serve", True),
        ("python -m http.server 8765", False),
        ("node server.js --port 8765", False),
        # Le piege : notre module, mais pas la commande `serve`.
        ("python -m assistant_vocal.cli doctor", False),
        ("", False),
    ],
)
def test_est_notre_moteur(ligne: str, attendu: bool):
    """La garde qui autorise a tuer sans demander : elle doit etre stricte."""
    assert est_notre_moteur(ligne) is attendu


def test_processus_du_port_lit_lsof_puis_ps(monkeypatch: pytest.MonkeyPatch):
    def faux_run(commande: list[str], **_kwargs: object) -> _Sortie:
        if commande[0] == "lsof":
            assert "-sTCP:LISTEN" in commande, "sans ce filtre, un client compte pour un serveur"
            return _Sortie("74311\n")
        if commande[0] == "ps":
            return _Sortie(NOTRE_MOTEUR + "\n")
        raise AssertionError(commande)

    monkeypatch.setattr("assistant_vocal.launcher.os.name", "posix")
    monkeypatch.setattr("assistant_vocal.launcher.shutil.which", lambda _: "/usr/sbin/lsof")
    monkeypatch.setattr("assistant_vocal.launcher.subprocess.run", faux_run)
    assert processus_du_port(8765) == (74311, NOTRE_MOTEUR)


def test_processus_du_port_prend_le_premier_pid(monkeypatch: pytest.MonkeyPatch):
    """`lsof -t` rend un PID par famille d'adresses : c'est le meme processus."""
    monkeypatch.setattr("assistant_vocal.launcher.os.name", "posix")
    monkeypatch.setattr("assistant_vocal.launcher.shutil.which", lambda _: "/usr/sbin/lsof")
    monkeypatch.setattr(
        "assistant_vocal.launcher.subprocess.run",
        lambda commande, **_k: _Sortie("42\n42\n" if commande[0] == "lsof" else NOTRE_MOTEUR),
    )
    trouve = processus_du_port(8765)
    assert trouve is not None
    assert trouve[0] == 42


def test_processus_du_port_rend_none_si_personne(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("assistant_vocal.launcher.os.name", "posix")
    monkeypatch.setattr("assistant_vocal.launcher.shutil.which", lambda _: "/usr/sbin/lsof")
    monkeypatch.setattr("assistant_vocal.launcher.subprocess.run", lambda *_a, **_k: _Sortie(""))
    assert processus_du_port(8765) is None


def _bouchonner_arret(monkeypatch: pytest.MonkeyPatch, vivant: object) -> list[int]:
    """Instrumente `arreter_pid` : renvoie la liste des signaux envoyes."""
    envoyes: list[int] = []
    monkeypatch.setattr(
        "assistant_vocal.launcher._signaler", lambda _pid, numero: envoyes.append(numero)
    )
    monkeypatch.setattr("assistant_vocal.launcher.pid_vivant", lambda _pid: vivant(envoyes))
    monkeypatch.setattr("assistant_vocal.launcher.time.monotonic", _Horloge())
    monkeypatch.setattr("assistant_vocal.launcher.time.sleep", lambda _: None)
    return envoyes


def test_arreter_pid_force_quand_sigterm_est_ignore(monkeypatch: pytest.MonkeyPatch):
    """LE correctif : la bibliotheque intercepte SIGTERM et peut ne jamais partir.

    L'ancien code faisait `return` des que le premier signal echouait ou restait
    sans effet. Le moteur survivait, le port restait pris.
    """
    envoyes = _bouchonner_arret(monkeypatch, lambda envoyes: signal.SIGKILL not in envoyes)
    assert arreter_pid(999, delai_s=5.0) is True
    assert envoyes == [signal.SIGTERM, signal.SIGKILL]


def test_arreter_pid_ne_force_pas_inutilement(monkeypatch: pytest.MonkeyPatch):
    """Un arret propre rend la memoire du GPU : ne pas SIGKILL sans raison."""
    envoyes = _bouchonner_arret(monkeypatch, lambda _envoyes: False)
    assert arreter_pid(999, delai_s=5.0) is True
    assert envoyes == [signal.SIGTERM]


def test_arreter_pid_avoue_son_echec(monkeypatch: pytest.MonkeyPatch):
    """Renvoyer False est ce qui permet a `down` de CONSERVER la trace du pid."""
    envoyes = _bouchonner_arret(monkeypatch, lambda _envoyes: True)
    assert arreter_pid(999, delai_s=5.0) is False
    assert envoyes == [signal.SIGTERM, signal.SIGKILL]


def test_signaler_ne_tue_pas_notre_propre_groupe(monkeypatch: pytest.MonkeyPatch):
    """Garde-fou : `killpg` sur notre groupe nous emporterait avec la cible.

    Le moteur a normalement son propre groupe (`start_new_session=True`), mais
    un PID retrouve par le port n'offre aucune garantie.
    """
    appels: list[tuple[str, int]] = []
    monkeypatch.setattr("assistant_vocal.launcher.os.name", "posix")
    monkeypatch.setattr("assistant_vocal.launcher.os.getpgid", lambda _pid: 4242)
    monkeypatch.setattr(
        "assistant_vocal.launcher.os.kill", lambda pid, _n: appels.append(("kill", pid))
    )
    monkeypatch.setattr(
        "assistant_vocal.launcher.os.killpg", lambda pgid, _n: appels.append(("killpg", pgid))
    )
    _signaler(999, signal.SIGTERM)
    assert appels == [("kill", 999)], "notre groupe : il faut viser le seul PID"


def test_signaler_vise_le_groupe_dun_moteur_isole(monkeypatch: pytest.MonkeyPatch):
    """Sinon un sous-processus essaime par l'inference survivrait."""
    appels: list[tuple[str, int]] = []
    monkeypatch.setattr("assistant_vocal.launcher.os.name", "posix")
    monkeypatch.setattr(
        "assistant_vocal.launcher.os.getpgid", lambda pid: 4242 if pid == 0 else 777
    )
    monkeypatch.setattr(
        "assistant_vocal.launcher.os.kill", lambda pid, _n: appels.append(("kill", pid))
    )
    monkeypatch.setattr(
        "assistant_vocal.launcher.os.killpg", lambda pgid, _n: appels.append(("killpg", pgid))
    )
    _signaler(999, signal.SIGTERM)
    assert appels == [("killpg", 777)]


def test_liberer_port_ne_touche_jamais_a_un_programme_tiers(monkeypatch: pytest.MonkeyPatch):
    """Un autre serveur sur 8765 doit etre signale, JAMAIS tue."""

    def interdit(*_a: object, **_k: object) -> bool:
        raise AssertionError("on ne tue pas un processus qui n'est pas a nous")

    monkeypatch.setattr(
        "assistant_vocal.launcher.processus_du_port", lambda _p: (4242, "node server.js")
    )
    monkeypatch.setattr("assistant_vocal.launcher.arreter_pid", interdit)
    assert liberer_port_moteur(8765) is False


def test_liberer_port_reprend_notre_moteur(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    arretes: list[int] = []
    monkeypatch.setattr(
        "assistant_vocal.launcher.processus_du_port", lambda _p: (74311, NOTRE_MOTEUR)
    )
    monkeypatch.setattr(
        "assistant_vocal.launcher.arreter_pid",
        lambda pid, **_k: bool(arretes.append(pid)) or True,
    )
    monkeypatch.setattr("assistant_vocal.launcher.port_libre", lambda _p: True)
    monkeypatch.setattr("assistant_vocal.launcher.FICHIER_PID", tmp_path / "backend.pid")
    assert liberer_port_moteur(8765) is True
    assert arretes == [74311]


def test_up_nomme_le_processus_qui_bloque_le_port(monkeypatch: pytest.MonkeyPatch):
    """Le message d'erreur doit dispenser de lancer `lsof` soi-meme.

    Et ne PLUS conseiller `assistant-vocal down` quand ce n'est pas lui qui
    peut aider : c'est ce conseil impossible qui faisait tourner en rond.
    """
    monkeypatch.setattr("assistant_vocal.launcher.port_libre", lambda port: port != 8765)
    monkeypatch.setattr("assistant_vocal.launcher.liberer_port_moteur", lambda _p: False)
    monkeypatch.setattr(
        "assistant_vocal.launcher.processus_du_port", lambda _p: (4242, "node server.js")
    )
    with pytest.raises(AssistantError) as capture:
        exiger_ports_libres(8765)
    message = str(capture.value)
    assert "4242" in message
    assert "node server.js" in message
    assert "VOICE_PORT" in message, "il faut offrir une sortie : changer de port"
    assert "assistant-vocal down" not in message


def test_up_dit_quoi_faire_si_notre_moteur_resiste(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("assistant_vocal.launcher.port_libre", lambda port: port != 8765)
    monkeypatch.setattr("assistant_vocal.launcher.liberer_port_moteur", lambda _p: False)
    monkeypatch.setattr(
        "assistant_vocal.launcher.processus_du_port", lambda _p: (74311, NOTRE_MOTEUR)
    )
    with pytest.raises(AssistantError, match="kill -9 74311"):
        exiger_ports_libres(8765)


# ---------------------------------------------------------------------------
# down : la trace du pid
# ---------------------------------------------------------------------------


def _bouchonner_down(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isole `down` de Docker et du port : il ne reste que la gestion du pid."""
    fichier = tmp_path / "backend.pid"
    monkeypatch.setattr("assistant_vocal.launcher.FICHIER_PID", fichier)
    monkeypatch.setattr("assistant_vocal.launcher._liberer_le_port_du_moteur", lambda: None)
    monkeypatch.setattr("assistant_vocal.launcher.compose", lambda *_a, **_k: _Sortie(""))
    return fichier


def test_down_conserve_la_trace_dun_moteur_qui_resiste(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """LE bug, dans sa moitie « perte de trace ».

    Effacer le fichier PID d'un processus encore vivant, c'est se priver de la
    seule prise qu'on avait sur lui.
    """
    fichier = _bouchonner_down(monkeypatch, tmp_path)
    fichier.write_text("74311", encoding="utf-8")
    monkeypatch.setattr("assistant_vocal.launcher._pid_enregistre", lambda: 74311)
    monkeypatch.setattr("assistant_vocal.launcher.arreter_pid", lambda *_a, **_k: False)
    assert down() == 0
    assert fichier.exists(), "la trace d'un processus vivant ne doit pas etre effacee"


def test_down_efface_la_trace_dun_moteur_bien_arrete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fichier = _bouchonner_down(monkeypatch, tmp_path)
    fichier.write_text("74311", encoding="utf-8")
    monkeypatch.setattr("assistant_vocal.launcher._pid_enregistre", lambda: 74311)
    monkeypatch.setattr("assistant_vocal.launcher.arreter_pid", lambda *_a, **_k: True)
    assert down() == 0
    assert not fichier.exists()


def test_down_nettoie_une_trace_perimee(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Un pid note qui ne correspond plus a rien : le fichier doit partir."""
    fichier = _bouchonner_down(monkeypatch, tmp_path)
    fichier.write_text("999999", encoding="utf-8")
    monkeypatch.setattr("assistant_vocal.launcher._pid_enregistre", lambda: None)
    assert down() == 0
    assert not fichier.exists()


def test_down_passe_par_le_port_meme_sans_fichier_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Le filet de securite : c'est le cas ou l'ancien `down` ne pouvait rien."""
    monkeypatch.setattr("assistant_vocal.launcher.FICHIER_PID", tmp_path / "absent.pid")
    monkeypatch.setattr("assistant_vocal.launcher.compose", lambda *_a, **_k: _Sortie(""))
    appele = []
    monkeypatch.setattr(
        "assistant_vocal.launcher._liberer_le_port_du_moteur", lambda: appele.append(True)
    )
    assert down() == 0
    assert appele == [True], "sans fichier PID, le port est la seule prise restante"


def test_pid_enregistre_ne_confond_pas_inaccessible_et_mort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """L'autre moitie du bug : PermissionError signifie « il existe ».

    L'ancien code rendait None, donc `down` effacait la trace d'un processus
    bien vivant.
    """
    fichier = tmp_path / "backend.pid"
    fichier.write_text("74311", encoding="utf-8")
    monkeypatch.setattr("assistant_vocal.launcher.FICHIER_PID", fichier)

    def refuse(_pid: int, _numero: int) -> None:
        raise PermissionError("pas a vous")

    monkeypatch.setattr("assistant_vocal.launcher.os.kill", refuse)
    assert _pid_enregistre() == 74311


def test_pid_enregistre_ignore_un_fichier_illisible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fichier = tmp_path / "backend.pid"
    fichier.write_text("ce n'est pas un nombre", encoding="utf-8")
    monkeypatch.setattr("assistant_vocal.launcher.FICHIER_PID", fichier)
    assert _pid_enregistre() is None
