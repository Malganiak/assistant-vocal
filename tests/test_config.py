"""Tests des reglages et de la ligne d'arguments produite.

Aucun modele, aucun micro, aucun reseau : ces tests doivent tourner en une
fraction de seconde.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from assistant_vocal.config import (
    SPEAKERS,
    VOIX_PATH,
    Settings,
    build_pipeline_args,
    inject_tts_gen_kwargs,
    read_prompt,
    read_ref_text,
    tts_gen_kwargs,
)

# ---------------------------------------------------------------------------
# Reglages
# ---------------------------------------------------------------------------


def test_defauts_sans_env():
    """Sans .env ni variable, on obtient la configuration locale documentee."""
    settings = Settings()
    assert settings.llm_mode == "local"
    assert settings.llm_model == "mlx-community/Qwen3-4B-Instruct-2507-4bit"
    assert settings.tts_speaker == "aiden"
    assert settings.tts_language == "fr"
    assert settings.host == "127.0.0.1"
    assert settings.allow_ui_prompt is False
    # Le defaut est le CLONAGE, donc un checkpoint Base, donc pas d'emotions :
    # le chemin de clonage ne transmet jamais `instruct`.
    assert settings.tts_model == "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit"
    assert settings.emotions is False


# ---------------------------------------------------------------------------
# Clonage de voix
# ---------------------------------------------------------------------------


def test_le_defaut_est_le_clonage_avec_la_voix_livree():
    """La voix francaise du paquet doit marcher sans aucune configuration."""
    settings = Settings()
    assert settings.type_de_modele == "base"
    assert settings.clonage_actif is True
    assert settings.ref_audio_path == VOIX_PATH
    assert settings.ref_audio_path.exists(), "la voix de reference doit etre livree"
    assert settings.ref_text == read_ref_text()
    assert settings.ref_text, "la transcription de reference ne peut pas etre vide"


@pytest.mark.parametrize(
    ("modele", "attendu"),
    [
        ("mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit", "base"),
        ("mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16", "base"),
        ("mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit", "custom_voice"),
        ("mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign", "voice_design"),
        ("Qwen/Qwen3-TTS-12Hz-0.6B-Base", "base"),
    ],
)
def test_type_de_modele_deduit_du_nom(modele: str, attendu: str):
    """Un seul reglage decide du mode : il n'y a pas deux boutons a accorder."""
    assert Settings(tts_model=modele).type_de_modele == attendu


def test_reference_sur_un_checkpoint_sans_clonage_est_refusee():
    """Le piege le plus couteux : il se traduirait par un silence a chaque replique.

    Le handler voit une reference, prend le chemin de clonage, appelle
    `generate()` sans locuteur -- et `generate()` sur un CustomVoice en exige un.
    L'exception est avalee par la bibliotheque.
    """
    with pytest.raises(ValidationError) as capture:
        Settings(
            tts_model="mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit",
            tts_ref_audio=str(VOIX_PATH),
            tts_ref_text="peu importe",
        )
    assert "ne sait pas cloner" in str(capture.value)


def test_audio_de_reference_introuvable_est_refuse_au_demarrage():
    with pytest.raises(ValidationError) as capture:
        Settings(tts_ref_audio="/nexiste/pas.wav", tts_ref_text="peu importe")
    assert "introuvable" in str(capture.value)


def test_audio_et_transcription_vont_par_paire():
    """Un audio sans sa transcription exacte desaligne le clonage."""
    with pytest.raises(ValidationError) as capture:
        Settings(tts_ref_audio=str(VOIX_PATH))
    assert "PAIRE" in str(capture.value)

    with pytest.raises(ValidationError) as capture:
        Settings(tts_ref_text="une transcription orpheline")
    assert "PAIRE" in str(capture.value)


def test_le_clonage_ne_passe_aucun_locuteur():
    """`--qwen3_tts_speaker` n'a aucun effet en clonage : ne pas le passer."""
    args = build_pipeline_args(Settings(), "local", mac_preset=True)
    assert "--qwen3_tts_ref_audio" in args
    assert "--qwen3_tts_ref_text" in args
    assert "--qwen3_tts_speaker" not in args
    # Le nom est passe avec son suffixe : sans lui, `_resolve_mlx_model_name`
    # completerait en `-6bit`, une variante qui n'existe pas pour Base.
    assert "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit" in args


def test_sans_clonage_le_locuteur_revient():
    settings = Settings(tts_model="mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit")
    args = build_pipeline_args(settings, "local", mac_preset=True)
    assert "--qwen3_tts_speaker" in args
    assert "--qwen3_tts_ref_audio" not in args


def test_locuteur_inconnu_est_refuse():
    """Un locuteur invalide doit echouer au demarrage, pas pendant la synthese.

    Sinon mlx-audio leve pendant la generation, la bibliotheque avale
    l'exception, et la replique disparait sans un son ni un message utile.
    """
    with pytest.raises(ValidationError) as capture:
        Settings(tts_speaker="Jacques")

    message = str(capture.value)
    assert "Jacques" in message
    # Le message doit lister les choix possibles : sinon l'utilisateur doit
    # aller lire le code pour savoir quoi ecrire.
    for locuteur in SPEAKERS:
        assert locuteur in message


def test_locuteur_normalise_la_casse():
    """L'UI de demo ecrit « Ono_Anna », le modele attend « ono_anna »."""
    assert Settings(tts_speaker="RYAN").tts_speaker == "ryan"
    assert Settings(tts_speaker="Ono_Anna").tts_speaker == "ono_anna"


def test_variables_denvironnement_prises_en_compte(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOICE_TTS_SPEAKER", "serena")
    monkeypatch.setenv("VOICE_TTS_TEMPERATURE", "0.7")
    settings = Settings()
    assert settings.tts_speaker == "serena"
    assert settings.tts_temperature == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Ligne d'arguments
# ---------------------------------------------------------------------------


def _paires(args: list[str]) -> dict[str, str | None]:
    """Transforme ['--a', '1', '--b'] en {'--a': '1', '--b': None}."""
    resultat: dict[str, str | None] = {}
    index = 0
    while index < len(args):
        drapeau = args[index]
        suivant = args[index + 1] if index + 1 < len(args) else None
        if suivant is not None and not suivant.startswith("--"):
            resultat[drapeau] = suivant
            index += 2
        else:
            resultat[drapeau] = None
            index += 1
    return resultat


def test_mode_local_choisit_mlx():
    args = _paires(build_pipeline_args(Settings(), "local", mac_preset=True))
    assert "--mac-optimal-settings" in args
    assert args["--llm_backend"] == "mlx-lm"
    assert args["--model_name"] == "mlx-community/Qwen3-4B-Instruct-2507-4bit"
    assert args["--tts"] == "qwen3"
    assert args["--qwen3_tts_language"] == "fr"
    assert args["--parakeet_tdt_language"] == "fr"


def test_mode_api_ne_passe_pas_de_peripherique():
    """En mode API il n'y a pas de modele local a placer sur un peripherique."""
    settings = Settings(llm_mode="api", api_base_url="http://127.0.0.1:8080/v1", api_key="x")
    args = _paires(build_pipeline_args(settings, "local", mac_preset=True))
    assert args["--llm_backend"] == "responses-api"
    assert args["--model_name"] == "gpt-5.6-terra"
    assert args["--responses_api_base_url"] == "http://127.0.0.1:8080/v1"
    assert "--llm_device" not in args


def test_mode_api_omet_les_champs_vides():
    """Une URL ou une cle vide ne doit pas produire un drapeau vide."""
    args = _paires(build_pipeline_args(Settings(llm_mode="api"), "local", mac_preset=True))
    assert "--responses_api_base_url" not in args
    assert "--responses_api_api_key" not in args


def test_host_seulement_pour_serve():
    """`--host` n'existe que pour la commande `serve`.

    La commande `local` tourne en boucle locale et n'expose qu'un `--port` :
    lui passer `--host` ferait echouer l'analyse des arguments.
    """
    local = _paires(build_pipeline_args(Settings(), "local", mac_preset=True))
    serve = _paires(build_pipeline_args(Settings(), "serve", mac_preset=True))
    assert "--host" not in local
    assert serve["--host"] == "127.0.0.1"


def test_options_audio_local_seulement_pour_local():
    """Symetriquement, `--local_audio_*` n'existe que pour la commande `local`."""
    local = _paires(build_pipeline_args(Settings(), "local", mac_preset=True))
    serve = _paires(build_pipeline_args(Settings(), "serve", mac_preset=True))
    assert "--local_audio_block_mic_during_playback" in local
    assert "--local_audio_block_mic_during_playback" not in serve


def test_micro_non_coupe_si_desactive():
    settings = Settings(block_mic_during_playback=False)
    args = _paires(build_pipeline_args(settings, "local", mac_preset=True))
    assert "--local_audio_block_mic_during_playback" not in args


def test_sans_prereglage_mac():
    """Sur Linux, le prereglage Apple Silicon ne doit pas etre passe."""
    args = _paires(build_pipeline_args(Settings(), "serve", mac_preset=False))
    assert "--mac-optimal-settings" not in args
    assert args["--stt"] == "parakeet-tdt"


def test_le_prompt_du_projet_est_passe():
    args = _paires(build_pipeline_args(Settings(), "local", mac_preset=True))
    assert args["--init_chat_prompt"] == read_prompt()
    assert args["--init_chat_role"] == "system"


# ---------------------------------------------------------------------------
# Le test le plus rentable du depot
# ---------------------------------------------------------------------------


def _champs_connus_de_la_bibliotheque() -> set[str]:
    """Tous les noms de champs declares par les dataclasses de la bibliotheque.

    On les decouvre en parcourant le paquet plutot qu'en listant les classes a
    la main : un renommage de classe en amont ne doit pas faire echouer ce test
    pour une mauvaise raison. Ce qu'on veut surveiller, ce sont les noms de
    CHAMPS, puisque ce sont eux qui deviennent les drapeaux.
    """
    import speech_to_speech.arguments_classes as paquet

    noms: set[str] = set()
    for info in pkgutil.iter_modules(paquet.__path__):
        module = importlib.import_module(f"{paquet.__name__}.{info.name}")
        for objet in vars(module).values():
            if dataclasses.is_dataclass(objet) and isinstance(objet, type):
                noms.update(champ.name for champ in dataclasses.fields(objet))
    return noms


def test_tous_les_drapeaux_existent():
    """Chaque drapeau produit doit correspondre a un champ de la bibliotheque.

    Pourquoi ce test compte plus que les autres : la bibliotheque n'echoue PAS
    sur un drapeau inconnu s'il appartient a un backend inactif. Elle le met de
    cote et journalise « Ignoring options for inactive backends ». Un drapeau
    mal orthographie, ou renomme lors d'une montee de version, serait donc
    ignore en silence -- et on chercherait pendant des heures pourquoi un
    reglage « ne fait rien ».

    Ce test transforme ce silence en echec au `pytest`.
    """
    connus = _champs_connus_de_la_bibliotheque()
    # Garde-fou : si la decouverte ne trouve rien, le test passerait a tort.
    assert len(connus) > 50, "la decouverte des dataclasses amont a echoue"

    produits: set[str] = set()
    for command in ("local", "serve"):
        for mode in ("local", "api"):
            settings = Settings(llm_mode=mode, api_base_url="http://x/v1", api_key="k")
            args = build_pipeline_args(settings, command, mac_preset=True)
            produits.update(a for a in args if a.startswith("--"))

    inconnus = {
        drapeau
        for drapeau in produits
        if drapeau.removeprefix("--").replace("-", "_") not in connus
    }
    assert not inconnus, f"drapeaux absents des dataclasses de la bibliotheque : {inconnus}"


# ---------------------------------------------------------------------------
# Injection des parametres d'echantillonnage
# ---------------------------------------------------------------------------


def test_gen_kwargs_contient_les_quatre_reglages():
    valeurs = tts_gen_kwargs(Settings(tts_temperature=0.8, tts_top_p=0.9))
    assert valeurs == {
        "temperature": 0.8,
        "top_k": 50,
        "top_p": 0.9,
        "repetition_penalty": 1.05,
    }
    # `streaming_context_size` ne doit PAS y figurer : la methode de synthese
    # CustomVoice ne l'accepte pas, et un argument de trop fait perdre la
    # replique en silence.
    assert "streaming_context_size" not in valeurs


@dataclass(frozen=True)
class _FausseSelection:
    """Imite `BackendSelection`, qui est frozen."""

    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FauxArguments:
    """Imite `ParsedArguments`, qui est mutable."""

    tts_backend: _FausseSelection


def test_injection_ajoute_gen_kwargs_sans_muter_loriginal():
    origine = _FausseSelection(config={"speaker": "aiden", "language": "fr"})
    args = _FauxArguments(tts_backend=origine)

    inject_tts_gen_kwargs(args, {"temperature": 0.8})

    # La nouvelle configuration porte gen_kwargs...
    assert args.tts_backend.config["gen_kwargs"] == {"temperature": 0.8}
    # ...sans avoir perdu le reste...
    assert args.tts_backend.config["speaker"] == "aiden"
    # ...et sans avoir modifie l'objet d'origine, qui est frozen.
    assert "gen_kwargs" not in origine.config


def test_injection_complete_un_gen_kwargs_existant():
    args = _FauxArguments(tts_backend=_FausseSelection(config={"gen_kwargs": {"top_k": 10}}))
    inject_tts_gen_kwargs(args, {"temperature": 0.8})
    assert args.tts_backend.config["gen_kwargs"] == {"top_k": 10, "temperature": 0.8}
