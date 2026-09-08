"""Demarrage conjoint du moteur d'inference et de l'UI de demo.

Le probleme que ce module resout : selon la plateforme, le moteur d'inference
ne tourne pas au meme endroit.

    macOS         moteur NATIF (via uv), UI en conteneur
    Linux+NVIDIA  moteur et UI, tous deux en conteneur

Pourquoi cette dissymetrie : Docker Desktop sur macOS fait tourner une VM Linux
sans acces a Metal, et le handler de synthese choisit son moteur en dur sur le
nom de la plateforme. Dans un conteneur sur un Mac, il basculerait donc sur le
chemin Linux en CPU pur, un ordre de grandeur trop lent pour une conversation.
Ce n'est pas un reglage a trouver : il n'existe aucun moyen d'exposer Metal a un
conteneur Linux.

`up()` masque cette difference : une seule commande, partout.

Pourquoi ce module est en Python et pas un Makefile ou un script shell : le
projet doit fonctionner sous Windows, ou il n'y a ni `make` ni shell POSIX. Un
script imposerait une seconde implementation PowerShell, donc la certitude
qu'elle divergerait.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from enum import StrEnum
from pathlib import Path

from assistant_vocal import AssistantError
from assistant_vocal.config import Settings

logger = logging.getLogger(__name__)

#: Racine du projet, deduite de l'emplacement de ce fichier.
RACINE = Path(__file__).resolve().parents[2]

#: Port de l'UI. Fixe dans compose.yaml : ce n'est pas un reglage.
PORT_UI = 7860

#: Ou l'on note le PID du moteur natif, pour que `down` sache quoi arreter si
#: `up` a ete tue brutalement.
FICHIER_PID = RACINE / ".assistant" / "backend.pid"


class Mode(StrEnum):
    """Ou tourne le moteur d'inference."""

    NATIF = "natif"
    GPU = "gpu"


# ---------------------------------------------------------------------------
# Sondes : plateforme, Docker, ports
# ---------------------------------------------------------------------------


def detecter_mode(force: str | None = None) -> Mode:
    """Choisit le mode d'execution, ou explique pourquoi aucun ne convient."""
    if force is not None:
        mode = Mode(force)
        if mode is Mode.GPU and sys.platform == "darwin":
            raise AssistantError(
                "le mode `gpu` ne peut pas fonctionner sur un Mac.\n\n"
                "  Docker Desktop y fait tourner une VM Linux sans acces a Metal. Le\n"
                "  moteur de synthese basculerait sur son chemin CPU, beaucoup trop\n"
                "  lent pour converser. Ce n'est pas un reglage manquant : il n'existe\n"
                "  aucun moyen d'exposer Metal a un conteneur Linux.\n\n"
                "  Sur ce Mac, utilisez simplement :  assistant-vocal up"
            )
        return mode

    if sys.platform == "darwin":
        if platform.machine() != "arm64":
            raise AssistantError(
                "ce projet demande un Mac Apple Silicon (puce M1 ou plus recente).\n"
                f"  Architecture detectee : {platform.machine()}.\n"
                "  Le moteur d'inference repose sur MLX, qui n'existe que pour ces puces."
            )
        return Mode.NATIF

    if shutil.which("nvidia-smi") is None:
        raise AssistantError(
            "aucun GPU NVIDIA detecte (la commande `nvidia-smi` est introuvable).\n\n"
            "  Sur Linux et Windows, le seul chemin rapide passe par un GPU NVIDIA.\n"
            "  Sans lui, l'inference tomberait sur le processeur : utilisable pour\n"
            "  verifier le cablage, pas pour tenir une conversation.\n\n"
            "  Pour forcer malgre tout, en connaissance de cause :\n"
            "      assistant-vocal up --mode natif"
        )

    return Mode.GPU


def exiger_docker(attente_s: float = 60.0) -> None:
    """Verifie que le daemon Docker repond, sinon explique quoi faire.

    Le piege : la commande `docker` existe et repond meme daemon eteint --
    `docker --version` affiche la version du CLIENT. On sonde donc le champ
    SERVEUR, et on teste le code de sortie ET la sortie vide.

    Ne JAMAIS mettre cette sonde dans un tube : `docker info | head` renvoie le
    code de sortie de `head`, donc zero, meme daemon eteint.

    On reessaie pendant `attente_s`, parce que sur macOS Docker Desktop demarre
    parfois de lui-meme quand une commande docker passe.
    """
    echeance = time.monotonic() + attente_s
    annonce_faite = False
    detail = "delai depasse"

    while True:
        try:
            resultat = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AssistantError(
                "la commande `docker` est introuvable.\n\n"
                "  macOS / Windows : installez Docker Desktop\n"
                "                    https://www.docker.com/products/docker-desktop\n"
                "  Linux           : https://docs.docker.com/engine/install/"
            ) from exc
        except subprocess.TimeoutExpired:
            resultat = None

        if resultat is not None and resultat.returncode == 0 and resultat.stdout.strip():
            logger.debug("daemon Docker pret (version %s)", resultat.stdout.strip())
            return

        if resultat is not None and resultat.stderr.strip():
            detail = resultat.stderr.strip().splitlines()[-1]

        if not annonce_faite:
            print(
                f"Le daemon Docker ne repond pas encore, j'attends jusqu'a {int(attente_s)} s\n"
                "  (Docker Desktop est peut-etre en train de demarrer)",
                file=sys.stderr,
            )
            annonce_faite = True

        if time.monotonic() >= echeance:
            raise AssistantError(
                "le daemon Docker ne repond pas.\n\n"
                f"  Detail : {detail}\n\n"
                "  Ce n'est pas une erreur du projet : le client docker est bien\n"
                "  installe, mais le moteur qui execute les conteneurs est arrete.\n\n"
                "  macOS   : ouvrez Docker Desktop, attendez que l'icone de la barre\n"
                "            de menus se stabilise, puis relancez la commande.\n"
                "            En ligne de commande :  open -a Docker\n"
                "  Linux   : sudo systemctl start docker\n"
                "            Si « permission denied » : sudo usermod -aG docker $USER\n"
                "            puis fermez et reouvrez votre session.\n"
                "  Windows : lancez Docker Desktop depuis le menu Demarrer.\n\n"
                "  Pour verifier vous-meme :  docker version\n"
                "  (la section « Server: » doit apparaitre)"
            )

        time.sleep(2.0)


def port_libre(port: int) -> bool:
    """Vrai si personne n'ecoute sur 127.0.0.1:port."""
    with socket.socket() as sonde:
        sonde.settimeout(0.5)
        return sonde.connect_ex(("127.0.0.1", port)) != 0


def exiger_ports_libres(port_moteur: int) -> None:
    """Echoue tot plutot que de laisser croire que tout va bien.

    C'est aussi ce qui rattrape un moteur orphelin, laisse derriere par un `up`
    tue brutalement : inutile d'aller fouiller les processus.
    """
    for port, quoi in ((port_moteur, "le moteur d'inference"), (PORT_UI, "l'UI de demo")):
        if port_libre(port):
            continue
        raise AssistantError(
            f"le port {port} est deja pris, or {quoi} en a besoin.\n\n"
            "  Soit une instance tourne encore :\n"
            "      assistant-vocal down\n"
            "  Soit un autre programme l'utilise :\n"
            f"      macOS / Linux : lsof -i :{port}\n"
            f"      Windows       : netstat -ano | findstr {port}"
        )


def attendre_http(
    url: str,
    *,
    libelle: str,
    delai_s: float,
    proc: subprocess.Popen[str] | None = None,
) -> None:
    """Attend que `url` reponde, en annoncant la progression.

    Si `proc` est fourni et meurt avant, on le dit tout de suite plutot que
    d'attendre le delai : la vraie cause est alors dans les lignes de journal
    qui precedent, sous les yeux de l'utilisateur.
    """
    debut = time.monotonic()
    prochain_rapport = debut + 15.0

    while True:
        if proc is not None and proc.poll() is not None:
            raise AssistantError(
                f"{libelle} s'est arrete avant d'etre pret (code {proc.returncode}).\n"
                "  La cause est dans les lignes « backend  | » ci-dessus."
            )
        try:
            with urllib.request.urlopen(url, timeout=3):
                print(f"{libelle} pret en {time.monotonic() - debut:.0f} s.", file=sys.stderr)
                return
        except (urllib.error.URLError, OSError, TimeoutError):
            pass

        maintenant = time.monotonic()
        if maintenant - debut > delai_s:
            raise AssistantError(f"{libelle} n'a pas repondu sur {url} en {delai_s:.0f} s.")
        if maintenant >= prochain_rapport:
            print(
                f"{libelle} demarre toujours ({int(maintenant - debut)} s)...",
                file=sys.stderr,
            )
            prochain_rapport = maintenant + 15.0
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# Le moteur natif, en sous-processus
# ---------------------------------------------------------------------------


def demarrer_backend_natif() -> subprocess.Popen[str]:
    """Lance le moteur d'inference dans un sous-processus Python.

    On utilise `sys.executable` et PAS `uv run` : `uv run` interpose un
    processus parent, donc le tuer laisserait un petit-fils orphelin -- un
    serveur qui garde le port et plusieurs gigaoctets de modeles en memoire.
    Comme `up` tourne deja dans l'environnement du projet, `sys.executable` EST
    le bon interpreteur. Un seul processus, aucun intermediaire, aucun orphelin
    possible.
    """
    options: dict[str, object] = {}
    if os.name == "posix":
        # Groupe de processus dedie : le Ctrl-C du terminal ne va pas
        # directement a l'enfant, c'est nous qui sequencons son arret.
        options["start_new_session"] = True
    else:
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "assistant_vocal.cli", "serve"],
        cwd=RACINE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **options,  # type: ignore[arg-type]
    )
    FICHIER_PID.parent.mkdir(parents=True, exist_ok=True)
    FICHIER_PID.write_text(str(proc.pid), encoding="utf-8")
    threading.Thread(target=_relayer_journaux, args=(proc,), daemon=True).start()
    return proc


def _relayer_journaux(proc: subprocess.Popen[str]) -> None:
    """Recopie la sortie du moteur natif en la prefixant.

    Format calque sur celui de `docker compose logs` (« ui-1  | ») pour que les
    deux flux se lisent comme un seul.
    """
    if proc.stdout is None:  # pragma: no cover - stdout est toujours un tube ici
        return
    for ligne in proc.stdout:
        sys.stdout.write(f"backend  | {ligne}")
        sys.stdout.flush()


def arreter_backend_natif(proc: subprocess.Popen[str], *, delai_s: float = 15.0) -> None:
    """Arret gracieux, puis brutal si besoin.

    SIGTERM est intercepte par la bibliotheque, qui arrete proprement ses fils
    d'execution : les handlers vident leurs files et rendent la memoire du GPU.
    On signale le GROUPE et pas seulement le PID : si l'inference a essaime un
    sous-processus, il part avec.
    """
    if proc.poll() is not None:
        return

    print("Arret du moteur d'inference...", file=sys.stderr)
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        return

    try:
        proc.wait(timeout=delai_s)
    except subprocess.TimeoutExpired:
        print("Le moteur ne repond pas, on force.", file=sys.stderr)
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=5.0)
        except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
            pass


# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------


def compose(*arguments: str, profil: str, strict: bool = True) -> subprocess.CompletedProcess[str]:
    """Appelle `docker compose` avec le profil demande, en affichant la commande.

    On montre la commande exacte : c'est ce qui permet de la rejouer a la main
    quand quelque chose cloche, et d'apprendre ce que le lanceur fait.

    `strict=True` transforme un echec en `AssistantError` -- donc en message
    lisible, pas en trace d'appel. `strict=False` rend simplement le resultat :
    c'est ce qu'il faut dans un `finally`, ou lever masquerait la cause
    d'origine.
    """
    commande = ["docker", "compose", *arguments]
    environnement = {**os.environ, "COMPOSE_PROFILES": profil}
    print(f"$ COMPOSE_PROFILES={profil} {' '.join(commande)}", file=sys.stderr)
    resultat = subprocess.run(commande, cwd=RACINE, env=environnement, check=False, text=True)
    if strict and resultat.returncode != 0:
        raise AssistantError(
            f"la commande docker ci-dessus a echoue (code {resultat.returncode}).\n"
            "  Le message de docker au-dessus est plus precis que tout ce que ce\n"
            "  projet pourrait en dire : c'est lui qu'il faut lire."
        )
    return resultat


def suivre_journaux(profil: str) -> subprocess.Popen[bytes]:
    """Lance `docker compose logs -f`, qui prefixe deja chaque ligne du service."""
    environnement = {**os.environ, "COMPOSE_PROFILES": profil}
    return subprocess.Popen(
        ["docker", "compose", "logs", "-f"],
        cwd=RACINE,
        env=environnement,
    )


# ---------------------------------------------------------------------------
# up / down
# ---------------------------------------------------------------------------


def _armer_signaux() -> None:
    """Fait qu'un Ctrl-C ou un `kill` deroulent tous les deux l'arret propre.

    Le gestionnaire leve `KeyboardInterrupt`, que `up()` attrape deja : un seul
    chemin d'arret a relire, quelle que soit la façon dont il est demande.
    """

    def demander_arret(numero: int, _cadre: object) -> None:
        raise KeyboardInterrupt(f"signal {numero}")

    for numero in (signal.SIGINT, signal.SIGTERM):
        # Poser un gestionnaire echoue hors du thread principal. Ce n'est pas
        # grave : on retombe alors sur le KeyboardInterrupt par defaut.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(numero, demander_arret)


def up(settings: Settings, *, force_mode: str | None = None, rebuild: bool = False) -> int:
    """Demarre le moteur et l'UI, journalise les deux, arrete tout au Ctrl-C."""
    mode = detecter_mode(force_mode)
    profil = "ui" if mode is Mode.NATIF else "gpu"

    print(f"Mode {mode.value} : profil compose « {profil} ».", file=sys.stderr)
    if mode is Mode.NATIF:
        print(
            "  Le moteur d'inference tourne en natif (MLX/Metal) ; seule l'UI est\n"
            "  en conteneur. Voir docs/DOCKER.md pour la raison.",
            file=sys.stderr,
        )

    exiger_docker()
    exiger_ports_libres(settings.port)
    # Le message de compose sur un fichier invalide est meilleur que tout ce
    # qu'on pourrait ecrire : on le laisse parler.
    compose("config", "-q", profil=profil)

    # Le cache Hugging Face de l'hote, calcule ici plutot que laisse a
    # l'interpolation de compose : `~` ne s'etend pas partout de la meme facon,
    # et sous Windows $HOME n'existe pas.
    os.environ.setdefault("VOICE_HF_CACHE", str(_cache_huggingface()))

    if rebuild:
        print(
            "Reconstruction des images. La premiere fois, comptez 3 a 8 minutes\n"
            "  pour l'UI et 10 a 25 pour le moteur CUDA.",
            file=sys.stderr,
        )

    # Gestionnaires explicites plutot que de compter sur le KeyboardInterrupt
    # que Python leve tout seul. Deux raisons :
    #   - SIGTERM (un `kill`, un superviseur) ne leverait rien du tout, et le
    #     bloc `finally` ne tournerait pas : conteneur et moteur resteraient en
    #     vie, ports occupes ;
    #   - un processus lance en arriere-plan depuis un shell non interactif
    #     herite de SIGINT IGNORE. Reinstaller le gestionnaire le rearme.
    _armer_signaux()

    backend: subprocess.Popen[str] | None = None
    suiveur: subprocess.Popen[bytes] | None = None
    try:
        if mode is Mode.NATIF:
            backend = demarrer_backend_natif()

        arguments = ["up", "-d"]
        if rebuild:
            arguments.append("--build")
        compose(*arguments, profil=profil)

        # Le suiveur demarre AVANT l'attente de disponibilite : au premier
        # lancement, le chargement des modeles prend du temps, et on veut que
        # ca defile plutot que de paraitre bloque.
        suiveur = suivre_journaux(profil)

        attendre_http(
            f"http://127.0.0.1:{settings.port}/v1/pool",
            libelle="Le moteur d'inference",
            delai_s=1800,
            proc=backend,
        )
        attendre_http(
            f"http://127.0.0.1:{PORT_UI}/api/config",
            libelle="L'UI de demo",
            delai_s=120,
        )

        print(
            f"\n  Tout est pret.  Ouvrez  http://localhost:{PORT_UI}/\n"
            "  Transport : WebSocket (le defaut de l'UI).\n"
            "  Ctrl-C pour arreter.\n",
            file=sys.stderr,
        )
        suiveur.wait()

    except KeyboardInterrupt:
        print("\nArret demande.", file=sys.stderr)
    finally:
        # Ordre voulu : on coupe le robinet de journaux, puis l'UI (pour que
        # personne ne se reconnecte a un moteur qui meurt), puis le moteur.
        if suiveur is not None and suiveur.poll() is None:
            suiveur.terminate()
        # `*` selectionne tous les profils : l'arret est complet quel que soit
        # celui qui etait actif. `strict=False` parce qu'une exception levee
        # dans un `finally` masquerait la cause d'origine.
        compose("down", "--remove-orphans", profil="*", strict=False)
        if backend is not None:
            arreter_backend_natif(backend)
        FICHIER_PID.unlink(missing_ok=True)

    return 0


def down() -> int:
    """Filet de securite : arrete ce qui traine encore.

    Fonctionne meme daemon eteint : le moteur natif est arrete d'abord, et
    l'echec de la partie Docker est signale sans faire echouer la commande.
    """
    pid = _pid_enregistre()
    if pid is not None:
        print(f"Arret du moteur natif laisse derriere (pid {pid}).", file=sys.stderr)
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:  # pragma: no cover - branche Windows
                os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            print("  Il n'existe plus.", file=sys.stderr)
        FICHIER_PID.unlink(missing_ok=True)

    # `down` doit rester utile meme sans Docker : le moteur natif, lui, vient
    # d'etre arrete. On ne fait donc pas echouer la commande pour autant.
    try:
        resultat = compose("down", "--remove-orphans", profil="*", strict=False)
    except FileNotFoundError:
        print("La commande `docker` est introuvable : rien a arreter de ce cote.", file=sys.stderr)
        return 0

    if resultat.returncode != 0:
        print(
            "La partie Docker n'a pas pu etre arretee (daemon eteint ?).\n"
            "  Relancez `assistant-vocal down` apres avoir demarre Docker.",
            file=sys.stderr,
        )
    return 0


def _pid_enregistre() -> int | None:
    """Le PID note par `up`, s'il correspond a un processus encore vivant."""
    try:
        pid = int(FICHIER_PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    return pid


def _cache_huggingface() -> Path:
    """Le dossier de cache des poids, en respectant les variables usuelles."""
    if valeur := os.environ.get("HF_HOME"):
        return Path(valeur)
    return Path.home() / ".cache" / "huggingface"
