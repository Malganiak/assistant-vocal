"""Reglages du projet et fabrication de la ligne d'arguments de la bibliotheque.

Ce module est volontairement PUR : il n'importe ni torch, ni mlx, ni
`speech_to_speech`. C'est ce qui permet a ses tests de tourner en une fraction
de seconde, sans charger un seul modele et sans ouvrir le micro.

Une seule classe de reglages, un seul niveau de champs. Toutes les variables
d'environnement sont prefixees VOICE_ et documentees dans .env.example.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from assistant_vocal import AssistantError

#: Les neuf locuteurs du modele Qwen3-TTS CustomVoice, tels que le modele les
#: declare. Un nom inconnu fait lever mlx-audio *pendant* la synthese, et cette
#: exception est avalee par la bibliotheque : la replique disparait alors sans
#: un son. On valide donc au demarrage, bruyamment.
SPEAKERS: tuple[str, ...] = (
    "aiden",
    "dylan",
    "eric",
    "ono_anna",
    "ryan",
    "serena",
    "sohee",
    "uncle_fu",
    "vivian",
)

#: Le prompt systeme vit dans un fichier texte a cote de ce module, pas dans une
#: constante Python : on peut le lire, l'editer et le coller dans l'UI de demo
#: sans se battre avec les echappements.
PROMPT_PATH = Path(__file__).with_name("prompt_fr.txt")

#: Les depots de poids utilises en mode local sur Apple Silicon. Le prereglage
#: `--mac-optimal-settings` et les defauts des handlers les choisissent sans
#: qu'on ait a les nommer ; on les liste ici pour que `doctor` puisse verifier
#: qu'ils sont bien en cache, et pour que le README n'ait pas a deviner.
MODELES_LOCAUX: dict[str, str] = {
    "transcription": "mlx-community/parakeet-tdt-0.6b-v3",
    "synthese": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit",
    "fin de tour": "pipecat-ai/smart-turn-v3",
}


class Settings(BaseSettings):
    """Tous les reglages du projet. Voir .env.example pour le detail de chacun."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="VOICE_",
        extra="ignore",
        frozen=True,
    )

    # --- Serveur --------------------------------------------------------------
    # 127.0.0.1 par defaut, et ce n'est pas de la prudence decorative : l'API
    # temps reel de la bibliotheque n'a AUCUNE authentification. 0.0.0.0 n'est
    # legitime que dans un conteneur, derriere le reseau Docker.
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    # --- LLM : local par defaut, bascule vers une API par une seule variable ---
    llm_mode: Literal["local", "api"] = "local"
    llm_model: str = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
    api_model: str = "gpt-5.6-terra"
    api_base_url: str = ""
    api_key: str = ""

    # --- Langue, voix ---------------------------------------------------------
    stt_language: str = "fr"
    tts_speaker: str = "aiden"
    tts_language: str = "fr"

    # --- Synthese : debit de sortie -------------------------------------------
    # Nombre de pas de codec par paquet audio. Mesure : chaque pas coute environ
    # 30 ms de latence avant le premier son, pour un debit de generation qui ne
    # bouge quasiment pas. 4 est le defaut de la bibliotheque et le meilleur
    # compromis mesure ; c'est aussi celui qui produit le moins de
    # discontinuites aux jointures de paquets.
    tts_chunk_size: int = 4
    tts_max_tokens: int = 1536

    # --- Synthese : echantillonnage -------------------------------------------
    # Ces quatre reglages n'ont AUCUNE option en ligne de commande dans la
    # bibliotheque. Ils passent par la rustine `gen_kwargs` de patches.py.
    tts_temperature: float = 0.9
    tts_top_k: int = 50
    tts_top_p: float = 1.0
    tts_repetition_penalty: float = 1.05

    # --- Comportement ---------------------------------------------------------
    # Une repartie courte tient en une ou deux phrases. Avec le defaut de 3 de
    # la bibliotheque, la synthese n'attaquerait qu'au tout dernier morceau
    # produit par le LLM. A 1, elle demarre des la premiere phrase -- et le
    # handler regroupe de lui-meme les phrases deja en file quand le LLM est en
    # avance, donc on ne perd pas la prosodie de groupe.
    batch_sentences: int = 1

    # Coupe le micro pendant que l'assistant parle. Sans annulation d'echo,
    # c'est indispensable sur haut-parleur, sinon l'assistant s'entend et se
    # coupe tout seul. Au casque, mettre false pour retrouver l'interruption a
    # la voix, qui rend l'echange bien plus vivant.
    block_mic_during_playback: bool = True

    # --- Emotions -------------------------------------------------------------
    emotions: bool = True
    # true : laisse l'UI de demo imposer son propre prompt systeme. Les
    # etiquettes d'emotion cessent alors d'arriver -- voir patches.py.
    allow_ui_prompt: bool = False

    @field_validator("tts_speaker")
    @classmethod
    def _locuteur_connu(cls, valeur: str) -> str:
        normalise = valeur.strip().lower()
        if normalise not in SPEAKERS:
            raise ValueError(f"locuteur inconnu {valeur!r} ; choisis parmi : {', '.join(SPEAKERS)}")
        return normalise


def read_prompt() -> str:
    """Le prompt systeme du projet, source unique du depot."""
    try:
        return PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover - ne peut arriver qu'en paquet casse
        raise AssistantError(f"prompt introuvable : {PROMPT_PATH}") from exc


def tts_gen_kwargs(settings: Settings) -> dict[str, float | int]:
    """Les parametres d'echantillonnage a injecter dans la config du backend TTS.

    Volontairement limite a ces quatre-la. `streaming_context_size` et `speed`
    existent aussi cote modele, mais seule la methode de clonage de voix les
    accepte : les passer au chemin CustomVoice leve un TypeError qui fait
    perdre la replique. Voir le filtre de signature dans tts_handler.py.
    """
    return {
        "temperature": settings.tts_temperature,
        "top_k": settings.tts_top_k,
        "top_p": settings.tts_top_p,
        "repetition_penalty": settings.tts_repetition_penalty,
    }


def build_pipeline_args(
    settings: Settings,
    command: Literal["local", "serve"],
    *,
    mac_preset: bool,
) -> list[str]:
    """Fabrique la liste d'arguments que la bibliotheque va analyser.

    Ecrite en Python plutot que dans un fichier de configuration pour une
    raison precise : la bibliotheque n'echoue PAS sur un drapeau inconnu si ce
    drapeau appartient a un backend inactif. Elle le range de cote et se
    contente d'un « Ignoring options for inactive backends ». Un
    `--language fr` (qui appartient au backend Whisper) est donc avale en
    silence alors qu'on croyait regler la langue de la synthese ; le bon
    drapeau est `--qwen3_tts_language`.

    D'ou le test `test_config.py::test_tous_les_drapeaux_existent`, qui verifie
    que chaque drapeau produit ici correspond bien a un champ des dataclasses
    de la bibliotheque. C'est ce test qui transforme un drapeau renomme en
    amont d'un silence a l'execution en un echec au `pytest`.

    `command` reprend le vocabulaire de la bibliotheque : `local` prend le
    micro et les haut-parleurs de la machine, `serve` n'expose que le serveur
    temps reel, pour l'UI web.
    """
    args: list[str] = []

    # --- Selection des moteurs ------------------------------------------------
    if mac_preset:
        # A ecrire avec des TIRETS : la bibliotheque retire volontairement la
        # variante `--mac_optimal_settings` de son analyseur. Ce prereglage
        # choisit Parakeet TDT, mlx-lm et Qwen3-TTS, tous sur `mps`.
        args += ["--mac-optimal-settings"]
    args += ["--stt", "parakeet-tdt"]
    args += ["--tts", "qwen3"]

    # --- Transcription --------------------------------------------------------
    args += ["--parakeet_tdt_language", settings.stt_language]

    # --- Modele de langue -----------------------------------------------------
    if settings.llm_mode == "local":
        args += ["--llm_backend", "mlx-lm"]
        args += ["--model_name", settings.llm_model]
    else:
        # En mode API, on ne passe surtout pas `--llm_device` : il n'y a pas de
        # modele local a placer sur un peripherique.
        args += ["--llm_backend", "responses-api"]
        args += ["--model_name", settings.api_model]
        if settings.api_base_url:
            args += ["--responses_api_base_url", settings.api_base_url]
        if settings.api_key:
            args += ["--responses_api_api_key", settings.api_key]

    # --- Synthese -------------------------------------------------------------
    # Un `args +=` par drapeau : chaque ligne garde le drapeau et sa valeur
    # ensemble, ce qui se relit et se modifie sans compter les elements.
    args += ["--qwen3_tts_speaker", settings.tts_speaker]
    args += ["--qwen3_tts_language", settings.tts_language]
    args += ["--qwen3_tts_streaming_chunk_size", str(settings.tts_chunk_size)]
    args += ["--qwen3_tts_max_new_tokens", str(settings.tts_max_tokens)]

    # --- Conversation ---------------------------------------------------------
    args += ["--enable_lang_prompt"]
    args += ["--init_chat_role", "system"]
    args += ["--init_chat_prompt", read_prompt()]
    args += ["--stream_batch_sentences", str(settings.batch_sentences)]

    # --- Serveur --------------------------------------------------------------
    args += ["--port", str(settings.port)]
    args += ["--log_level", settings.log_level]
    if command == "serve":
        # `--host` n'existe QUE pour la commande `serve`. La commande `local`
        # tourne en boucle locale et n'expose qu'un `--port`.
        args += ["--host", settings.host]
    elif settings.block_mic_during_playback:
        # Symetriquement, les options `--local_audio_*` ne sont enregistrees
        # que pour la commande `local`. Les passer a `serve` ferait echouer
        # l'analyse des arguments.
        args += ["--local_audio_block_mic_during_playback"]

    return args


def inject_tts_gen_kwargs(args: Any, gen_kwargs: dict[str, float | int]) -> None:
    """Ajoute `gen_kwargs` a la configuration du backend de synthese.

    `args` est un `ParsedArguments` de la bibliotheque, mais il est duck-type a
    dessein : ce module ne doit pas importer `s2s_pipeline`, dont l'import
    charge torch et telecharge des ressources NLTK. Le test peut donc passer un
    faux objet.

    `ParsedArguments` est une dataclass MUTABLE, mais `BackendSelection` est
    FROZEN : d'ou le `dataclasses.replace` plutot qu'une affectation directe
    dans la configuration.
    """
    selection = args.tts_backend
    config = dict(selection.config)
    config["gen_kwargs"] = {**config.get("gen_kwargs", {}), **gen_kwargs}
    args.tts_backend = replace(selection, config=config)
