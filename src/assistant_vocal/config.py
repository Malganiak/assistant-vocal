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

from pydantic import field_validator, model_validator
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

#: La voix de reference du clonage, livree avec le paquet. Meme raisonnement que
#: pour le prompt : un fichier a cote du module, pas une ressource a aller
#: chercher ailleurs. `uv_build` embarque tout le contenu de src/assistant_vocal.
#:
#: Le clonage a besoin des DEUX : l'audio, et sa transcription EXACTE. Une
#: transcription approximative degrade la voix produite, parce que le modele
#: aligne le texte de reference sur l'audio de reference pour en deduire le
#: timbre. C'est aussi pour ca que les deux fichiers portent le meme nom.
VOIX_PATH = Path(__file__).with_name("voix_francaise.wav")
VOIX_TEXTE_PATH = Path(__file__).with_name("voix_francaise.txt")

#: Les trois familles de checkpoints Qwen3-TTS, et ce que chacune sait faire.
#: On deduit la famille du NOM du modele, exactement comme le fait la
#: bibliotheque dans `_infer_model_type_from_name`. Un seul reglage decide donc
#: du mode de synthese : il n'y a pas deux boutons a garder coherents.
#:
#:   base          clonage de voix, a partir d'un audio de reference
#:   custom_voice  un des neuf locuteurs predefinis
#:   voice_design  une voix decrite en langage naturel
TypeDeModele = Literal["base", "custom_voice", "voice_design"]

#: Les depots de poids utilises en mode local sur Apple Silicon. Le prereglage
#: `--mac-optimal-settings` et les defauts des handlers les choisissent sans
#: qu'on ait a les nommer ; on les liste ici pour que `doctor` puisse verifier
#: qu'ils sont bien en cache, et pour que le README n'ait pas a deviner.
MODELES_LOCAUX: dict[str, str] = {
    "transcription": "mlx-community/parakeet-tdt-0.6b-v3",
    "synthese": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
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

    # --- Langue ---------------------------------------------------------------
    stt_language: str = "fr"
    tts_language: str = "fr"

    # --- Synthese : quel modele, donc quel mode de voix -----------------------
    # Le checkpoint Base est le SEUL des trois qui sache cloner une voix. C'est
    # ce qui permet d'avoir une voix reellement francaise : aucun des neuf
    # locuteurs de CustomVoice n'est natif du francais (serena et vivian sont
    # chinoises, ono_anna japonaise, sohee coreenne, aiden et ryan anglaises), et
    # la documentation amont recommande d'utiliser chaque locuteur dans SA langue.
    #
    # POURQUOI 8bit et pas bf16, mesure a l'appui : les deux produisent le meme
    # nombre de pas a une unite pres (65 contre 66 sur la meme phrase), donc la
    # quantification ne change pas le contenu genere. Mais bf16 tourne a RTF
    # 0,59-0,66 -- PLUS LENT que le temps reel, donc inutilisable pour une
    # conversation -- quand 8bit tient RTF 1,33-2,59. Et il economise 1,3 Go.
    tts_model: str = "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit"

    # Vide = la reference livree avec le paquet (VOIX_PATH / VOIX_TEXTE_PATH).
    # Renseigner les deux ensemble pour utiliser une autre voix : l'audio SANS sa
    # transcription exacte degrade le resultat.
    tts_ref_audio: str = ""
    tts_ref_text: str = ""

    # Locuteur predefini. IGNORE en clonage (le checkpoint Base n'en a aucun) :
    # il ne sert que si vous repassez sur un checkpoint CustomVoice.
    tts_speaker: str = "aiden"

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
    # false PAR DEFAUT, parce que le clonage de voix et les emotions sont
    # MUTUELLEMENT EXCLUSIFS -- ce n'est pas un choix de gout, c'est une limite
    # du modele. Le ton d'une replique se demande par le parametre `instruct`,
    # or le chemin de clonage ne le transmet jamais : `_process_voice_clone`
    # passe text, ref_audio, ref_text et lang_code, rien d'autre. mlx-audio est
    # d'ailleurs explicite ailleurs dans son code : « Qwen3-TTS batch reference
    # cloning does not support instructs ».
    #
    # Ce que false NE desactive PAS : le retrait des etiquettes. `EmotionTracker`
    # continue de les enlever du texte, et c'est indispensable -- une etiquette
    # laissee dans le texte se ferait PRONONCER. Seule l'application du ton
    # s'arrete.
    #
    # Repassez-le a true si vous revenez sur un checkpoint CustomVoice.
    emotions: bool = False
    # true : laisse l'UI de demo imposer son propre prompt systeme. Les
    # etiquettes d'emotion cessent alors d'arriver -- voir patches.py.
    allow_ui_prompt: bool = False

    @property
    def type_de_modele(self) -> TypeDeModele:
        """La famille du checkpoint, deduite de son nom.

        Reproduit VOLONTAIREMENT `_infer_model_type_from_name` de la
        bibliotheque, y compris l'ordre des tests : c'est elle qui decidera du
        chemin de synthese, et une divergence entre sa lecture et la notre
        rendrait la validation ci-dessous mensongere.
        `test_config.py` verifie que les deux restent d'accord.
        """
        nom = self.tts_model.lower()
        if "voicedesign" in nom:
            return "voice_design"
        if "customvoice" in nom:
            return "custom_voice"
        return "base"

    @property
    def clonage_actif(self) -> bool:
        """Vrai quand la synthese clone la voix de reference.

        Seul le checkpoint Base sait le faire ; c'est aussi le seul qui l'exige,
        puisqu'il n'a aucun locuteur predefini.
        """
        return self.type_de_modele == "base"

    @property
    def ref_audio_path(self) -> Path:
        """L'audio de reference : celui du paquet, ou celui qu'on a demande."""
        if not self.tts_ref_audio:
            return VOIX_PATH
        # Absolu : `_resolve_audio_path` de la bibliotheque ne cherche qu'au
        # repertoire courant et a la racine du paquet AMONT. Un chemin relatif
        # ne serait donc trouve que si l'on lance depuis la racine du depot.
        return Path(self.tts_ref_audio).expanduser().resolve()

    @property
    def ref_text(self) -> str:
        """La transcription de reference : celle du paquet, ou celle demandee."""
        return self.tts_ref_text or read_ref_text()

    @field_validator("tts_speaker")
    @classmethod
    def _locuteur_connu(cls, valeur: str) -> str:
        normalise = valeur.strip().lower()
        if normalise not in SPEAKERS:
            raise ValueError(f"locuteur inconnu {valeur!r} ; choisis parmi : {', '.join(SPEAKERS)}")
        return normalise

    @model_validator(mode="after")
    def _reference_coherente(self) -> Settings:
        """Refuse au demarrage les combinaisons qui echouent en silence.

        MEME RAISON QUE `_locuteur_connu`, et le meme symptome a eviter : le
        `process()` de la bibliotheque enveloppe toute la synthese dans un
        `except Exception` qui journalise « Error during Qwen3-TTS generation ».
        Une erreur de configuration ne se voit donc PAS -- la replique
        disparait, sans un son.

        Le piege que ce validateur attrape vraiment : un audio de reference
        pose sur un checkpoint CustomVoice. Le handler voit une reference, prend
        le chemin de clonage, appelle `generate()` SANS locuteur -- et
        `generate()`, sur un CustomVoice, exige un locuteur. Levee, avalee,
        silence a chaque replique. Rien dans le journal ne pointerait vers la
        vraie cause.
        """
        if self.clonage_actif:
            if not self.ref_audio_path.exists():
                raise ValueError(
                    f"audio de reference introuvable : {self.ref_audio_path}\n"
                    f"  Le checkpoint {self.tts_model} est un modele Base : il n'a aucun\n"
                    f"  locuteur predefini et ne peut parler QUE par clonage."
                )
            if not self.ref_text.strip():
                raise ValueError(
                    "transcription de reference vide. Le clonage aligne le texte de "
                    "reference sur l'audio de reference : sans elle, la voix produite "
                    "est degradee."
                )
        elif self.tts_ref_audio:
            raise ValueError(
                f"VOICE_TTS_REF_AUDIO est renseigne, mais {self.tts_model} n'est pas un\n"
                f"  modele Base ({self.type_de_modele}) : il ne sait pas cloner une voix.\n"
                f"  La bibliotheque prendrait quand meme le chemin de clonage et echouerait\n"
                f"  en silence, une replique perdue sur deux lignes de journal.\n\n"
                f"  Soit VOICE_TTS_MODEL=mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit,\n"
                f"  soit laissez VOICE_TTS_REF_AUDIO vide."
            )

        if bool(self.tts_ref_audio) != bool(self.tts_ref_text):
            raise ValueError(
                "VOICE_TTS_REF_AUDIO et VOICE_TTS_REF_TEXT vont par PAIRE. Un audio sans "
                "sa transcription exacte -- ou l'inverse -- desaligne le clonage et "
                "degrade la voix. Laissez les deux vides pour la voix livree."
            )
        return self


def read_prompt() -> str:
    """Le prompt systeme du projet, source unique du depot."""
    try:
        return PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover - ne peut arriver qu'en paquet casse
        raise AssistantError(f"prompt introuvable : {PROMPT_PATH}") from exc


def read_ref_text() -> str:
    """La transcription de la voix de reference livree avec le paquet."""
    try:
        return VOIX_TEXTE_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover - ne peut arriver qu'en paquet casse
        raise AssistantError(f"transcription de reference introuvable : {VOIX_TEXTE_PATH}") from exc


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
    #
    # Le nom du modele est passe EXPLICITEMENT, avec son suffixe de
    # quantification. Deux raisons : `--mac-optimal-settings` ne fixe que des
    # valeurs par DEFAUT (`parser.set_defaults`), donc un drapeau explicite
    # gagne ; et surtout, un nom sans suffixe se verrait completer en `-6bit` par
    # `_resolve_mlx_model_name`, une variante qui n'existe pas pour le
    # checkpoint Base et qu'il faudrait telecharger.
    args += ["--qwen3_tts_model_name", settings.tts_model]

    if settings.clonage_actif:
        # Le chemin de clonage n'utilise AUCUN locuteur : ne pas passer
        # `--qwen3_tts_speaker` ici evite de laisser croire, dans la sortie de
        # `doctor`, qu'il aurait un effet.
        args += ["--qwen3_tts_ref_audio", str(settings.ref_audio_path)]
        args += ["--qwen3_tts_ref_text", settings.ref_text]
    else:
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
