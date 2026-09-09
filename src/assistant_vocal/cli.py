"""Point d'entree en ligne de commande. Doit se lire d'un trait.

assistant-vocal doctor    verifier l'installation, sans rien charger
assistant-vocal prompt    afficher le prompt systeme (--copy pour le copier)
assistant-vocal run       parler au micro de la machine, sans Docker
assistant-vocal serve     lancer le serveur temps reel, pour l'UI web
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from assistant_vocal import AssistantError, __version__
from assistant_vocal.config import (
    MODELES_LOCAUX,
    Settings,
    build_pipeline_args,
    read_prompt,
)


def main(argv: list[str] | None = None) -> int:
    """Analyse la commande demandee et l'execute."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return args.execute(args)
    except AssistantError as exc:
        # Erreur previsible : on montre le message, pas la pile. Une trace
        # d'appel n'apprend rien a personne quand le probleme est « Docker
        # n'est pas demarre ».
        print(f"\nErreur : {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assistant-vocal",
        description="Assistant vocal francophone bati sur speech-to-speech.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="commande", required=True, metavar="COMMANDE")

    p_doctor = sub.add_parser("doctor", help="verifier l'installation, sans rien charger")
    p_doctor.set_defaults(execute=cmd_doctor)

    p_prompt = sub.add_parser("prompt", help="afficher le prompt systeme du projet")
    p_prompt.add_argument(
        "--copy",
        action="store_true",
        help="copier dans le presse-papier au lieu de l'afficher",
    )
    p_prompt.set_defaults(execute=cmd_prompt)

    p_run = sub.add_parser("run", help="parler au micro de la machine, sans Docker")
    p_run.set_defaults(execute=cmd_run)

    p_serve = sub.add_parser("serve", help="lancer le serveur temps reel, pour l'UI web")
    p_serve.set_defaults(execute=cmd_serve)

    p_up = sub.add_parser("up", help="tout demarrer : moteur + UI dans le navigateur")
    p_up.add_argument(
        "--mode",
        choices=["natif", "gpu"],
        default=None,
        help="forcer l'emplacement du moteur (par defaut : deduit de la plateforme)",
    )
    p_up.add_argument(
        "--rebuild",
        action="store_true",
        help="reconstruire les images docker (necessite le reseau)",
    )
    p_up.set_defaults(execute=cmd_up)

    p_down = sub.add_parser("down", help="arreter ce qui traine encore")
    p_down.set_defaults(execute=cmd_down)

    return parser


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Verification a sec : aucun modele charge, aucun micro ouvert.

    C'est la commande a lancer en premier quand quelque chose ne marche pas.
    Elle doit rendre la main en moins de deux secondes.
    """
    settings = Settings()

    print(f"assistant-vocal {__version__}")
    print(f"  python      {platform.python_version()}")
    print(f"  plateforme  {sys.platform} / {platform.machine()}")

    print("\nReglages resolus")
    for champ, valeur in settings.model_dump().items():
        # La cle d'API ne doit jamais apparaitre en clair, meme dans un
        # diagnostic que l'on copie-colle dans une issue.
        if champ == "api_key":
            valeur = "(definie)" if valeur else "(vide)"
        print(f"  {champ:28s} {valeur}")

    print("\nVoix")
    for ligne in _lignes_voix(settings):
        print(f"  {ligne}")

    print("\nPoids attendus en mode local")
    manquants = _modeles_manquants(settings)
    if manquants:
        print("\n  Ces poids ne sont pas dans le cache : ils seront telecharges au")
        print("  premier lancement. Comptez plusieurs gigaoctets.")

    print("\nLigne d'arguments passee a la bibliotheque (commande `run`)")
    mac = sys.platform == "darwin"
    for morceau in _lignes_lisibles(build_pipeline_args(settings, "local", mac_preset=mac)):
        print(f"  {morceau}")

    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    """Affiche le prompt systeme, ou le met dans le presse-papier."""
    prompt = read_prompt()
    if not args.copy:
        print(prompt)
        return 0

    outil = _outil_presse_papier()
    if outil is None:
        raise AssistantError(
            "aucun outil de presse-papier trouve (pbcopy, wl-copy, xclip ou clip).\n"
            "  Affichez le prompt et copiez-le a la main :\n"
            "      assistant-vocal prompt"
        )
    subprocess.run(outil, input=prompt, text=True, check=True)
    print(f"Prompt copie dans le presse-papier ({len(prompt)} caracteres).")
    return 0


def cmd_run(_args: argparse.Namespace) -> int:
    """Micro et haut-parleurs de la machine, sans conteneur ni navigateur.

    C'est le chemin le plus court pour verifier que tout fonctionne.
    """
    return _lancer("local")


def cmd_serve(_args: argparse.Namespace) -> int:
    """Serveur temps reel seul, a destination de l'UI de demo.

    C'est le point d'accroche du conteneur : sur macOS cette commande tourne en
    natif (Docker n'a pas acces a Metal), sur Linux avec un GPU NVIDIA la meme
    commande tourne dans le conteneur CUDA. Rien ne change dans le code : seuls
    VOICE_HOST et VOICE_PORT diffèrent.
    """
    return _lancer("serve")


def cmd_up(args: argparse.Namespace) -> int:
    """Demarre le moteur et l'UI de demo, et fusionne leurs journaux.

    C'est la commande a retenir : elle marche a l'identique sur macOS, Linux et
    Windows, en s'adaptant a ce que la plateforme sait faire.
    """
    from assistant_vocal.launcher import up

    return up(Settings(), force_mode=args.mode, rebuild=args.rebuild)


def cmd_down(_args: argparse.Namespace) -> int:
    """Arrete les conteneurs et le moteur natif eventuellement laisse derriere."""
    from assistant_vocal.launcher import down

    return down()


# ---------------------------------------------------------------------------
# Plomberie
# ---------------------------------------------------------------------------


def _lancer(command: str) -> int:
    """Prepare la configuration puis rend la main a la bibliotheque.

    L'import de `speech_to_speech` est tardif a dessein : il charge torch et
    telecharge des ressources NLTK. `doctor` et `prompt` doivent rester
    instantanes.
    """
    settings = Settings()

    # `run_pipeline_command` pose ses propres gestionnaires SIGINT et SIGTERM et
    # arrete proprement ses fils d'execution : il n'y a rien a ajouter cote
    # signaux. On l'appelle plutot que le script console `speech-to-speech`
    # pour rester dans un seul processus Python.
    from speech_to_speech.s2s_pipeline import run_pipeline_command

    from assistant_vocal import patches

    # Les rustines doivent etre posees AVANT que le pipeline ne construise ses
    # handlers. Voir patches.py pour ce qu'elles font et pourquoi.
    patches.install(settings)

    args = build_pipeline_args(settings, command, mac_preset=sys.platform == "darwin")
    run_pipeline_command(command, args)
    return 0


def _lignes_voix(settings: Settings) -> list[str]:
    """Comment la voix est produite. C'est devenu le reglage le plus structurant.

    Sans ce bloc, il faut croiser trois lignes de « Reglages resolus » pour
    savoir si l'on clone ou non -- et le piege qu'on veut rendre visible est
    justement une incoherence entre le checkpoint et la reference.
    """
    if not settings.clonage_actif:
        return [
            f"{'mode':14s} locuteur predefini ({settings.type_de_modele})",
            f"{'locuteur':14s} {settings.tts_speaker}",
        ]

    lignes = [f"{'mode':14s} clonage de voix (checkpoint Base)"]

    chemin = settings.ref_audio_path
    detail = ""
    try:
        import wave

        with wave.open(str(chemin)) as fichier:
            hz = fichier.getframerate()
            duree = fichier.getnframes() / hz
            detail = f"  [{duree:.1f} s, {hz} Hz, {fichier.getnchannels()} canal]"
    except Exception:
        # Un WAV illisible n'empeche pas de diagnostiquer le reste. La
        # bibliotheque sait lire d'autres formats que `wave` : l'absence de
        # detail n'est donc pas un defaut en soi.
        detail = "  [duree non lue]"

    lignes.append(f"{'reference':14s} {chemin}{detail}")
    lignes.append(f"{'transcription':14s} {len(settings.ref_text)} caracteres")
    lignes.append(f"{'locuteur':14s} (sans effet en clonage)")
    if settings.emotions:
        lignes.append(
            f"{'emotions':14s} ACTIVEES, mais SANS EFFET : le chemin de clonage "
            "ne transmet pas `instruct`"
        )
    else:
        lignes.append(f"{'emotions':14s} desactivees (incompatibles avec le clonage)")
    return lignes


def _modeles_manquants(settings: Settings) -> list[str]:
    """Affiche l'etat de chaque depot de poids et renvoie les manquants."""
    from huggingface_hub.constants import HF_HUB_CACHE

    attendus = dict(MODELES_LOCAUX)
    # La synthese est un REGLAGE depuis l'arrivee du clonage : verifier le
    # modele fige de MODELES_LOCAUX ferait mentir `doctor` des que quelqu'un
    # change VOICE_TTS_MODEL.
    attendus["synthese"] = settings.tts_model
    if settings.llm_mode == "local":
        attendus["langue"] = settings.llm_model

    manquants: list[str] = []
    for role, depot in attendus.items():
        chemin = Path(HF_HUB_CACHE) / f"models--{depot.replace('/', '--')}"
        # Un dossier peut exister sans contenu : HuggingFace laisse une coquille
        # vide derriere un telechargement interrompu. On regarde donc s'il y a
        # bien un instantane peuple.
        instantanes = list((chemin / "snapshots").glob("*/*")) if chemin.is_dir() else []
        if instantanes:
            print(f"  {role:14s} {depot}  [en cache]")
        else:
            print(f"  {role:14s} {depot}  [ABSENT]")
            manquants.append(depot)
    return manquants


def _lignes_lisibles(args: list[str]) -> list[str]:
    """Regroupe une liste d'arguments en lignes « --drapeau valeur ».

    Le prompt systeme fait plus de mille caracteres : on le tronque, sinon le
    diagnostic devient illisible.
    """
    lignes: list[str] = []
    index = 0
    while index < len(args):
        drapeau = args[index]
        suivant = args[index + 1] if index + 1 < len(args) else None
        if suivant is not None and not suivant.startswith("--"):
            valeur = suivant if len(suivant) <= 60 else suivant[:57].replace("\n", " ") + "..."
            lignes.append(f"{drapeau} {valeur}")
            index += 2
        else:
            lignes.append(drapeau)
            index += 1
    return lignes


def _outil_presse_papier() -> list[str] | None:
    """La commande de copie disponible sur cette machine, ou None."""
    candidats = [["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip"]]
    for commande in candidats:
        if shutil.which(commande[0]) is not None:
            return commande
    return None


if __name__ == "__main__":
    sys.exit(main())
