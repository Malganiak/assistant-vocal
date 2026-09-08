"""Tests des rustines et du filtre d'arguments.

Ce module importe `speech_to_speech`, donc torch : il coute une poignee de
secondes au premier import, une fois pour toute la session. Aucun modele n'est
charge et aucun micro n'est ouvert.

Les deux tests marques SENTINELLE sont les plus importants du depot : ils
verifient le MECANISME sur lequel les rustines reposent. Si l'un d'eux echoue
apres une montee de version de la bibliotheque, la rustine correspondante est
devenue inoperante -- silencieusement, en production.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from assistant_vocal.tts_handler import filtrer_sur_signature, parametres_acceptes

# ---------------------------------------------------------------------------
# Filtre de signature
# ---------------------------------------------------------------------------


def _signature_fermee(text: str, speaker: str, temperature: float = 0.9) -> dict[str, object]:
    """Imite `generate_custom_voice()` : aucun `**kwargs`."""
    return {"text": text, "speaker": speaker, "temperature": temperature}


def _signature_ouverte(text: str, **kwargs: object) -> dict[str, object]:
    """Imite `generate()` : accepte tout."""
    return {"text": text, **kwargs}


def test_parametres_acceptes_signature_fermee():
    assert parametres_acceptes(_signature_fermee) == {"text", "speaker", "temperature"}


def test_parametres_acceptes_signature_ouverte():
    """None signifie « accepte tout » : il n'y a rien a filtrer."""
    assert parametres_acceptes(_signature_ouverte) is None


def test_signature_ouverte_nest_pas_enveloppee():
    """Inutile de payer une enveloppe quand la fonction accepte tout."""
    assert filtrer_sur_signature(_signature_ouverte) is _signature_ouverte


def test_argument_en_trop_ecarte_et_signale(caplog: pytest.LogCaptureFixture):
    """C'est le bug reel qui faisait perdre la replique sans un son."""
    filtree = filtrer_sur_signature(_signature_fermee)

    with caplog.at_level(logging.DEBUG, logger="assistant_vocal.tts_handler"):
        resultat = filtree(
            text="Bonjour",
            speaker="aiden",
            temperature=0.8,
            streaming_context_size=25,  # accepte par generate(), pas par celle-ci
        )

    # L'appel aboutit au lieu de lever un TypeError...
    assert resultat == {"text": "Bonjour", "speaker": "aiden", "temperature": 0.8}
    # ...et l'argument ecarte est journalise, pour qu'on puisse le corriger.
    assert [e for e in caplog.records if "streaming_context_size" in e.message]


def test_les_arguments_legitimes_passent_intacts():
    filtree = filtrer_sur_signature(_signature_fermee)
    assert filtree(text="Salut", speaker="vivian", temperature=0.7) == {
        "text": "Salut",
        "speaker": "vivian",
        "temperature": 0.7,
    }


def test_le_nom_de_la_fonction_survit_a_lenveloppe():
    """La bibliotheque journalise le nom : il ne doit pas devenir « enveloppe »."""
    assert filtrer_sur_signature(_signature_fermee).__name__ == "_signature_fermee"


# ---------------------------------------------------------------------------
# SENTINELLE de la rustine 1 : la classe est-elle resolue paresseusement ?
# ---------------------------------------------------------------------------


def test_sentinelle_la_classe_du_handler_est_resolue_par_getattr(
    monkeypatch: pytest.MonkeyPatch,
):
    """La rustine 1 ne tient que si la bibliotheque resout la classe par son nom.

    Elle le fait aujourd'hui : sa fabrique importe le module puis fait un
    `getattr` au moment de construire le handler. Si elle passait a un import
    direct en tete de fichier, remplacer l'attribut du module deviendrait un
    NO-OP SILENCIEUX : ni emotions, ni filtre d'arguments, et rien dans le
    journal pour le dire.

    Ce test transforme ce silence en echec au `pytest`.
    """
    import speech_to_speech.TTS.qwen3_tts_handler as module
    from speech_to_speech.backend_registry import _load_handler

    class Temoin:
        pass

    monkeypatch.setattr(module, "Qwen3TTSHandler", Temoin)

    resolue = _load_handler("speech_to_speech.TTS.qwen3_tts_handler", "Qwen3TTSHandler")
    assert resolue is Temoin, (
        "la bibliotheque ne resout plus la classe du handler par getattr : "
        "la rustine 1 de patches.py est devenue inoperante"
    )


# ---------------------------------------------------------------------------
# SENTINELLE de la rustine 3 : le prompt du projet survit-il au client ?
# ---------------------------------------------------------------------------


@pytest.fixture
def rustine_prompt_posee() -> Iterator[None]:
    """Pose la rustine 3 et la retire ensuite, pour ne pas contaminer les autres tests."""
    from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig

    from assistant_vocal.patches import _rustine_3_prompt_du_projet

    original = RuntimeConfig.apply_session_update
    _rustine_3_prompt_du_projet()
    try:
        yield
    finally:
        RuntimeConfig.apply_session_update = original


def _config_avec_prompt(prompt: str):
    from openai.types.realtime.realtime_session_create_request import (
        RealtimeSessionCreateRequest,
    )
    from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig
    from speech_to_speech.LLM.chat import Chat

    return RuntimeConfig(
        chat=Chat(30),
        session=RealtimeSessionCreateRequest(type="realtime", instructions=prompt),
    )


def test_sentinelle_le_prompt_du_projet_survit(
    rustine_prompt_posee: None, caplog: pytest.LogCaptureFixture
):
    """L'UI de demo envoie TOUJOURS son propre prompt : il doit etre ignore.

    Sans cette rustine, brancher l'UI ecrase le prompt du projet et les
    etiquettes d'emotion cessent d'arriver.
    """
    from openai.types.realtime.realtime_session_create_request import (
        RealtimeSessionCreateRequest,
    )

    config = _config_avec_prompt("PROMPT DU PROJET")

    with caplog.at_level(logging.INFO, logger="assistant_vocal.patches"):
        config.apply_session_update(
            RealtimeSessionCreateRequest(
                type="realtime",
                instructions="You are a friendly voice assistant.",
            )
        )

    assert config.session.instructions == "PROMPT DU PROJET"
    assert [e for e in caplog.records if "ignorees" in e.message]


def test_les_autres_champs_fusionnent_toujours(rustine_prompt_posee: None):
    """On ne retire QUE les instructions : la voix doit continuer de passer.

    C'est important : le menu deroulant de l'UI choisit le locuteur, et ce
    reglage-la doit rester fonctionnel.
    """
    from openai.types.realtime.realtime_session_create_request import (
        RealtimeSessionCreateRequest,
    )

    config = _config_avec_prompt("PROMPT DU PROJET")
    config.apply_session_update(
        RealtimeSessionCreateRequest(
            type="realtime",
            instructions="ignore-moi",
            audio={"output": {"voice": "ryan"}},
        )
    )

    assert config.session.instructions == "PROMPT DU PROJET"
    assert config.session.audio is not None
    assert config.session.audio.output is not None
    assert config.session.audio.output.voice == "ryan"


def test_sans_la_rustine_le_client_gagne():
    """L'echappatoire VOICE_ALLOW_UI_PROMPT doit vraiment rendre la main a l'UI."""
    from openai.types.realtime.realtime_session_create_request import (
        RealtimeSessionCreateRequest,
    )

    config = _config_avec_prompt("PROMPT DU PROJET")
    config.apply_session_update(
        RealtimeSessionCreateRequest(type="realtime", instructions="prompt du client")
    )
    assert config.session.instructions == "prompt du client"


def test_install_respecte_allow_ui_prompt(monkeypatch: pytest.MonkeyPatch):
    """`install()` ne doit poser la rustine 3 que si on ne l'a pas desactivee."""
    import assistant_vocal.patches as patches
    from assistant_vocal.config import Settings

    posees: list[str] = []
    monkeypatch.setattr(patches, "_POSEES", False)
    monkeypatch.setattr(patches, "_rustine_1_classe_du_handler", lambda s: posees.append("1"))
    monkeypatch.setattr(
        patches, "_rustine_2_parametres_echantillonnage", lambda s: posees.append("2")
    )
    monkeypatch.setattr(patches, "_rustine_3_prompt_du_projet", lambda: posees.append("3"))

    patches.install(Settings(allow_ui_prompt=True))
    assert posees == ["1", "2"]

    posees.clear()
    monkeypatch.setattr(patches, "_POSEES", False)
    patches.install(Settings(allow_ui_prompt=False))
    assert posees == ["1", "2", "3"]


def test_install_est_idempotent(monkeypatch: pytest.MonkeyPatch):
    """Deux appels ne doivent pas empiler deux enveloppes sur prepare_all_args."""
    import assistant_vocal.patches as patches
    from assistant_vocal.config import Settings

    appels: list[str] = []
    monkeypatch.setattr(patches, "_POSEES", False)
    monkeypatch.setattr(patches, "_rustine_1_classe_du_handler", lambda s: appels.append("1"))
    monkeypatch.setattr(
        patches, "_rustine_2_parametres_echantillonnage", lambda s: appels.append("2")
    )
    monkeypatch.setattr(patches, "_rustine_3_prompt_du_projet", lambda: appels.append("3"))

    settings = Settings()
    patches.install(settings)
    patches.install(settings)
    assert appels == ["1", "2", "3"]
