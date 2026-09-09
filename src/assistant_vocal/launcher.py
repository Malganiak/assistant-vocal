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
# Sondes : plateforme, Docker, ports, processus
#
# Le bloc « ports, processus » repond a un besoin precis : retrouver et arreter
# un moteur qu'on ne suit PLUS. Le fichier PID ne suffit pas -- un `up`
# interrompu brutalement laisse un orphelin vivant et perd sa trace. Le port,
# lui, ne mente jamais.
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


#: Ce qui identifie NOTRE moteur dans une ligne de commande. `up` ne lance rien
#: d'autre que `python -m assistant_vocal.cli serve` : ces deux marqueurs
#: ensemble ne peuvent designer qu'un moteur de ce projet.
_MARQUEURS_MOTEUR = ("assistant_vocal.cli", "serve")


def processus_du_port(port: int) -> tuple[int, str] | None:
    """Le PID qui ECOUTE sur `port`, avec sa ligne de commande, ou None.

    Pourquoi ne pas se contenter du fichier PID : un `up` interrompu brutalement
    laisse un moteur orphelin ET perd sa trace, parce que son `finally` n'a pas
    tourne -- ou a tourne a moitie. Le port, lui, ne mente jamais. C'est la
    seule source fiable pour retrouver un moteur qu'on ne suit plus.
    """
    if os.name != "posix":  # pragma: no cover - branche Windows
        return _processus_du_port_windows(port)

    if shutil.which("lsof") is None:  # pragma: no cover - lsof est partout sur macOS et Linux
        return None
    try:
        sortie = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None

    # `lsof -t` peut rendre plusieurs PID (IPv4 et IPv6 du meme serveur). Le
    # premier suffit : c'est le meme processus.
    pids = [morceau for morceau in sortie.split() if morceau.isdigit()]
    if not pids:
        return None
    pid = int(pids[0])
    return pid, ligne_de_commande(pid)


def _processus_du_port_windows(port: int) -> tuple[int, str] | None:  # pragma: no cover
    """Meme service, avec les outils que Windows a d'origine."""
    try:
        sortie = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=10.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for ligne in sortie.splitlines():
        morceaux = ligne.split()
        if len(morceaux) < 5 or morceaux[3] != "LISTENING":
            continue
        if morceaux[1].endswith(f":{port}") and morceaux[4].isdigit():
            pid = int(morceaux[4])
            return pid, ligne_de_commande(pid)
    return None


def ligne_de_commande(pid: int) -> str:
    """La commande du processus `pid`, ou une chaine vide si on ne sait pas.

    Elle sert a deux choses : decider si le processus est a nous, et le NOMMER
    dans le message d'erreur quand il ne l'est pas.
    """
    if os.name != "posix":  # pragma: no cover - branche Windows
        commande = ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"]
    else:
        commande = ["ps", "-p", str(pid), "-o", "command="]
    try:
        return subprocess.run(commande, capture_output=True, text=True, timeout=10.0).stdout.strip()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return ""


def est_notre_moteur(ligne: str) -> bool:
    """Vrai si cette ligne de commande est un moteur lance par ce projet.

    C'est la garde qui autorise `up` et `down` a tuer sans rien demander. Un
    serveur tiers qui utiliserait 8765 ne correspond pas, et on n'y touche
    JAMAIS : on se contente de le nommer dans le message d'erreur.
    """
    return all(marqueur in ligne for marqueur in _MARQUEURS_MOTEUR)


def pid_vivant(pid: int) -> bool:
    """Vrai si le processus existe encore."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # Il existe, mais il n'est pas a nous. On n'en fera rien, et le dire
        # vivant est la reponse honnete.
        return True
    return True


def _signaler(pid: int, numero: int) -> None:
    """Signale le GROUPE du processus -- sauf si c'est le notre.

    Le moteur est lance avec `start_new_session=True` : il a donc son propre
    groupe, et signaler le groupe emporte les sous-processus que l'inference
    aurait essaimes. Mais si le PID retrouve partageait NOTRE groupe, `killpg`
    nous tuerait avec lui : on retombe alors sur un signal au seul PID.
    """
    if os.name != "posix":  # pragma: no cover - branche Windows
        os.kill(pid, numero)
        return
    groupe = os.getpgid(pid)
    if groupe == os.getpgid(0):
        os.kill(pid, numero)
    else:
        os.killpg(groupe, numero)


def arreter_pid(pid: int, *, delai_s: float = 15.0) -> bool:
    """SIGTERM, puis SIGKILL. Vrai si le processus est bien parti.

    Version « PID nu » de `arreter_backend_natif`, pour les cas ou l'on n'a pas
    d'objet `Popen` : un moteur orphelin retrouve par son port, ou le pid note
    par un `up` d'une autre session.

    L'echec du PREMIER signal ne fait PAS abandonner, et c'est le coeur du
    correctif : la bibliotheque intercepte SIGTERM et peut ne jamais rendre la
    main. Abandonner la laissait vivante, le port pris.
    """
    for numero, delai in ((signal.SIGTERM, delai_s), (signal.SIGKILL, 5.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            _signaler(pid, numero)

        fin = time.monotonic() + delai
        while time.monotonic() < fin:
            if not pid_vivant(pid):
                return True
            time.sleep(0.2)
        if numero is signal.SIGTERM:
            print("  Pas de reponse a SIGTERM, on force.", file=sys.stderr)
    return not pid_vivant(pid)


def liberer_port_moteur(port: int) -> bool:
    """Recupere le port si c'est un de NOS moteurs qui le tient.

    Vrai si le port est libre en sortant. Un programme tiers est signale par
    l'appelant, jamais tue : c'est `est_notre_moteur` qui trace la limite.

    Le delai de grace est court, a dessein : un orphelin n'a plus de session a
    fermer proprement, et faire attendre quinze secondes a chaque `up` serait
    payer cher un arret propre qui n'a plus d'objet.
    """
    trouve = processus_du_port(port)
    if trouve is None:
        return port_libre(port)

    pid, ligne = trouve
    if not est_notre_moteur(ligne):
        return False

    print(
        f"Un moteur de ce projet tient encore le port {port} (pid {pid}) : on l'arrete.",
        file=sys.stderr,
    )
    arreter_pid(pid, delai_s=5.0)
    if _pid_enregistre() is None:
        # La trace notee designe un processus mort : elle est perimee.
        FICHIER_PID.unlink(missing_ok=True)
    return port_libre(port)


def exiger_ports_libres(port_moteur: int) -> None:
    """Echoue tot plutot que de laisser croire que tout va bien.

    Avant d'echouer, on RECUPERE le port du moteur. Sans cela, un `up`
    interrompu brutalement laissait un orphelin, et le message conseillait
    `assistant-vocal down` -- la seule chose qui ne pouvait pas marcher,
    puisque le fichier PID avait disparu avec le `up`. Il fallait tuer le
    processus a la main, `up` apres `up`.
    """
    if not port_libre(port_moteur):
        liberer_port_moteur(port_moteur)

    for port, quoi in ((port_moteur, "le moteur d'inference"), (PORT_UI, "l'UI de demo")):
        if port_libre(port):
            continue

        # Nommer le coupable, et dire POURQUOI il n'a pas ete arrete tout seul.
        # C'est ce qui evite d'avoir a lancer `lsof` soi-meme, et surtout de
        # relancer `down` en boucle quand ce n'est pas lui qui peut aider.
        trouve = processus_du_port(port)
        if trouve is None:
            detail = (
                "  Impossible de savoir qui le tient.\n"
                f"      macOS / Linux : lsof -i :{port}\n"
                f"      Windows       : netstat -ano | findstr {port}"
            )
        else:
            pid, ligne = trouve
            detail = f"  Tenu par le pid {pid} :\n      {ligne or '(commande inconnue)'}\n"
            if est_notre_moteur(ligne):
                detail += (
                    "\n  C'est un moteur de ce projet, mais il n'a pas pu etre arrete.\n"
                    f"      kill -9 {pid}"
                )
            elif port == PORT_UI:
                detail += (
                    "\n  Ce n'est pas l'UI de ce projet. Un conteneur oublie ?\n      docker ps"
                )
            else:
                detail += (
                    "\n  Ce n'est pas un moteur de ce projet : on n'y touche pas.\n"
                    "  Arretez ce programme, ou choisissez un autre port :\n"
                    "      VOICE_PORT=8766 uv run assistant-vocal up"
                )

        raise AssistantError(f"le port {port} est deja pris, or {quoi} en a besoin.\n\n{detail}")


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

    L'echec du premier signal ne fait plus abandonner. Un `return` a cet endroit
    laissait un moteur bien vivant, et le `finally` de `up` effacait ensuite son
    pid : port pris, plus aucune trace, et `down` sans prise dessus.
    """
    if proc.poll() is not None:
        return

    print("Arret du moteur d'inference...", file=sys.stderr)
    if not arreter_pid(proc.pid, delai_s=delai_s):
        print(
            f"  Le moteur (pid {proc.pid}) resiste. Son pid reste note :\n"
            "  `assistant-vocal down` le reprendra, par son pid ou par son port.",
            file=sys.stderr,
        )
    # Recolter le zombie, pour que `proc.poll()` dise la verite juste apres.
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=1.0)


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
        # On effacait le fichier PID SANS regarder si le moteur etait vraiment
        # parti. Un moteur qui survit a son arret perdait donc sa trace, et
        # `down` n'avait plus rien pour le retrouver : le port restait pris
        # jusqu'a un `kill` a la main. On ne l'efface plus que s'il n'y a
        # effectivement plus rien a suivre.
        if backend is None or backend.poll() is not None:
            FICHIER_PID.unlink(missing_ok=True)

    return 0


def down() -> int:
    """Filet de securite : arrete ce qui traine encore.

    DEUX chemins, dans cet ordre, parce que le premier peut manquer :

      1. le pid note par `up` ;
      2. le PORT, qui ne mente jamais.

    Le second est le filet qui manquait. Sans lui, un moteur orphelin dont le
    fichier PID avait disparu ne pouvait etre arrete que par un `kill` a la
    main -- alors que `up` conseillait precisement `down`.

    Fonctionne meme daemon eteint : le moteur natif est arrete d'abord, et
    l'echec de la partie Docker est signale sans faire echouer la commande.
    """
    pid = _pid_enregistre()
    if pid is not None:
        print(f"Arret du moteur natif laisse derriere (pid {pid}).", file=sys.stderr)
        if arreter_pid(pid):
            FICHIER_PID.unlink(missing_ok=True)
        else:
            # Ne PAS effacer la trace d'un processus encore vivant : c'est
            # exactement ce qui le rendait introuvable au `down` suivant.
            print(
                f"  Le pid {pid} n'a pas pu etre arrete ; sa trace est conservee.",
                file=sys.stderr,
            )
    elif FICHIER_PID.exists():
        # Le fichier designe un processus mort : trace perimee, on la retire.
        FICHIER_PID.unlink(missing_ok=True)

    _liberer_le_port_du_moteur()

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


def _liberer_le_port_du_moteur() -> None:
    """Le filet de securite de `down` : rattraper un moteur par son PORT.

    Le fichier PID peut avoir disparu alors que le moteur vit encore. Le port
    est alors la seule prise qui reste.
    """
    try:
        port = Settings().port
    except Exception:
        # `down` doit marcher meme avec un .env casse : on ne peut pas exiger
        # une configuration valide pour avoir le droit d'arreter quelque chose.
        port = int(Settings.model_fields["port"].default)

    if port_libre(port):
        return

    if liberer_port_moteur(port):
        return

    if (trouve := processus_du_port(port)) is None:
        return
    autre_pid, ligne = trouve
    if est_notre_moteur(ligne):
        print(
            f"Le moteur (pid {autre_pid}) tient toujours le port {port} :\n"
            f"      kill -9 {autre_pid}",
            file=sys.stderr,
        )
    else:
        print(
            f"Le port {port} est tenu par le pid {autre_pid}, qui n'est PAS un moteur\n"
            f"  de ce projet -- on n'y touche pas :\n      {ligne or '(commande inconnue)'}",
            file=sys.stderr,
        )


def _pid_enregistre() -> int | None:
    """Le PID note par `up`, s'il correspond a un processus encore vivant.

    `pid_vivant` et pas un `os.kill` local : un PermissionError signifie que le
    processus EXISTE mais nous echappe, et le confondre avec « il n'existe
    plus » etait l'autre moitie du bug -- la trace etait alors effacee.
    """
    try:
        pid = int(FICHIER_PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid_vivant(pid) else None


def _cache_huggingface() -> Path:
    """Le dossier de cache des poids, en respectant les variables usuelles."""
    if valeur := os.environ.get("HF_HOME"):
        return Path(valeur)
    return Path.home() / ".cache" / "huggingface"
