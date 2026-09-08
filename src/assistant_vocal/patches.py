"""Les trois rustines posees dans `speech-to-speech`, et pourquoi.

C'est le fichier a relire en premier apres chaque

    uv lock --upgrade-package speech-to-speech

Chaque rustine porte trois choses : ce qu'elle fait, POURQUOI la bibliotheque
ne permet pas de faire autrement, et CE QUI LA CASSE.

Aucune rustine ne modifie un fichier du paquet : on remplace des attributs en
memoire, au demarrage. Il n'y a donc rien a re-appliquer apres une
reinstallation, et `uv sync --reinstall` ne casse rien.

Deux sentinelles disent si les rustines s'appliquent encore :

  - la ligne « synthese avec emotions active » au demarrage (rustine 1) ;
  - l'avertissement « aucune etiquette d'emotion » a la premiere replique
    (rustines 1 et 3).

Si l'une des deux change de comportement apres une montee de version, c'est ici
qu'il faut regarder.
"""

from __future__ import annotations

import logging
from typing import Any

from assistant_vocal.config import Settings, tts_gen_kwargs

logger = logging.getLogger(__name__)

#: `install()` peut etre appele plusieurs fois dans un meme processus (les tests
#: le font). Sans ce garde-fou, les enveloppes s'empileraient.
_POSEES = False


def install(settings: Settings) -> None:
    """Pose les rustines. A appeler AVANT que le pipeline ne soit construit."""
    global _POSEES
    if _POSEES:
        return
    _POSEES = True

    _rustine_1_classe_du_handler(settings)
    _rustine_2_parametres_echantillonnage(settings)
    if not settings.allow_ui_prompt:
        _rustine_3_prompt_du_projet()


# ---------------------------------------------------------------------------
# Rustine 1 : substituer la classe du handler de synthese
# ---------------------------------------------------------------------------


def _rustine_1_classe_du_handler(settings: Settings) -> None:
    """Remplace `Qwen3TTSHandler` par notre sous-classe, dans son module.

    POURQUOI une seule ligne suffit : la fabrique du registre de la
    bibliotheque resout la classe PARESSEUSEMENT, par son nom, au moment de
    construire le handler (`import_module` puis `getattr`). Elle ne la capture
    pas a l'import. Remplacer l'attribut du module est donc suffisant, et il n'y
    a aucune structure de registre a reconstruire.

    CE QUI LA CASSE : si le registre passait a un import direct en tete de
    fichier, le nom serait deja lie et cette ligne deviendrait un NO-OP
    SILENCIEUX -- plus d'emotions, plus de filtre d'arguments, et rien dans le
    journal. C'est pour ce scenario precis qu'existent les deux sentinelles
    citees en tete de module.
    """
    import speech_to_speech.TTS.qwen3_tts_handler as module

    from assistant_vocal.tts_handler import Qwen3TTSHandlerAvecEmotions

    Qwen3TTSHandlerAvecEmotions.appliquer_emotions = settings.emotions
    module.Qwen3TTSHandler = Qwen3TTSHandlerAvecEmotions


# ---------------------------------------------------------------------------
# Rustine 2 : injecter les parametres d'echantillonnage de la synthese
# ---------------------------------------------------------------------------


def _rustine_2_parametres_echantillonnage(settings: Settings) -> None:
    """Glisse temperature, top_k, top_p et repetition_penalty dans la config TTS.

    POURQUOI : ces quatre reglages n'ont AUCUNE option en ligne de commande. Le
    handler les transmet pourtant a `model.generate*` via son attribut
    `gen_kwargs`, lui-meme alimente par tout champ de dataclasse prefixe `gen_`.
    Or la dataclasse de configuration de Qwen3-TTS n'a aucun champ `gen_*` : le
    dictionnaire arrive donc toujours vide, et il n'existe pas de moyen
    documente de regler l'echantillonnage.

    POURQUOI ICI : `prepare_all_args` est appele juste apres l'analyse des
    arguments et juste avant la construction du pipeline. C'est la derniere
    fenetre ou la configuration existe et est encore modifiable. On enveloppe
    cette fonction plutot que de recopier `run_pipeline_command`, qui gere aussi
    les signaux, l'arret propre et le pool de pipelines : zero logique dupliquee.

    POURQUOI CA MARCHE : `run_pipeline_command` resout `prepare_all_args` dans
    les globales de son module a chaque appel. Remplacer l'attribut du module
    suffit donc.

    SANS EFFET SUR LINUX/CUDA : le chemin `faster-qwen3-tts` appelle une methode
    de streaming a la liste d'arguments fermee, qui n'inclut pas `gen_kwargs`.
    Ce n'est pas une regression, c'est une limite a connaitre.

    CE QUI LA CASSE :
      - l'ajout de vrais champs `qwen3_tts_gen_*` en amont. Bonne nouvelle en
        soi, mais nos valeurs prendraient alors le pas sur celles de la ligne de
        commande : il faudrait supprimer cette rustine ;
      - un renommage de `prepare_all_args` ;
      - `ParsedArguments` devenant `frozen` (il est mutable aujourd'hui, alors
        que `BackendSelection` est deja frozen -- d'ou le `dataclasses.replace`
        dans `config.inject_tts_gen_kwargs`).
    """
    import speech_to_speech.s2s_pipeline as pipeline

    from assistant_vocal.config import inject_tts_gen_kwargs

    original = pipeline.prepare_all_args
    valeurs = tts_gen_kwargs(settings)

    def prepare_all_args(args: Any) -> None:
        original(args)
        if args.tts_backend.name != "qwen3":
            return
        inject_tts_gen_kwargs(args, valeurs)
        logger.info("parametres d'echantillonnage de la synthese : %s", valeurs)

    pipeline.prepare_all_args = prepare_all_args


# ---------------------------------------------------------------------------
# Rustine 3 : proteger le prompt systeme du projet
# ---------------------------------------------------------------------------


def _rustine_3_prompt_du_projet() -> None:
    """Empeche le client (l'UI de demo) d'ecraser le prompt systeme du projet.

    LE PROBLEME : l'UI de demo definit son propre prompt par defaut (« You are a
    friendly voice assistant. ») et envoie TOUJOURS un champ `instructions` dans
    son `session.update`. Or `session.instructions` est exactement ce que
    `--init_chat_prompt` alimente, et la construction du message systeme est
    refaite depuis cette valeur a CHAQUE reponse, un message systeme ajoute au
    fil de discussion remplacant le precedent. Des que l'UI est branchee, le
    prompt du projet disparait donc -- et avec lui les etiquettes d'emotion.
    Le `/api/config` de la demo n'expose aucun reglage d'instructions par
    defaut : aucune variable d'environnement ne corrige cela.

    POURQUOI ICI : `apply_session_update` est le SEUL endroit ou les
    instructions du client entrent dans l'etat du serveur, et elle n'a qu'un
    seul appelant. Une seule methode, en amont des DEUX hierarchies de handlers
    LLM : la rustine couvre donc `mlx-lm`, `transformers`, `responses-api` et
    `chat-completions`, en WebSocket comme en WebRTC. Rustiner la construction
    du prompt aurait demande de le faire dans deux espaces de noms distincts.

    COMMENT : la fusion n'itere que sur `update.model_fields_set`, et pydantic
    renvoie la l'ensemble VIVANT des champs explicitement fournis. En retirer
    « instructions » fait disparaitre ce champ de la fusion, sans toucher au
    reste : la voix, les outils et la detection de tour continuent de fusionner
    normalement.

    On journalise a chaque fois : le champ « Instructions » de l'UI devient
    decoratif, et il vaut mieux le dire que de mentir en silence.
    `VOICE_ALLOW_UI_PROMPT=true` desactive la rustine.

    CE QUI LA CASSE : un renommage d'`apply_session_update` ; une fusion qui
    n'utiliserait plus `model_fields_set` ; une version de pydantic rendant cet
    ensemble immuable. Dans les trois cas, l'avertissement « aucune etiquette
    d'emotion » le signalera des la premiere replique.
    """
    from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig

    original = RuntimeConfig.apply_session_update

    def apply_session_update(self: Any, update: Any) -> None:
        if "instructions" in update.model_fields_set:
            update.model_fields_set.discard("instructions")
            logger.info(
                "instructions envoyees par le client ignorees : le prompt du projet "
                "fait autorite (VOICE_ALLOW_UI_PROMPT=true pour changer cela)"
            )
        original(self, update)

    RuntimeConfig.apply_session_update = apply_session_update
