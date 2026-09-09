"""Tests de la ligne de commande.

Ce module n'importe ni torch, ni mlx, ni `speech_to_speech` : `cli.py` garde ses
imports lourds a l'interieur des fonctions qui en ont besoin, exactement pour
que `doctor` reste instantane. Ces tests verifient donc aussi, par leur simple
duree, que cette discipline tient.

AUCUN test ici n'execute `run`, `serve`, `up` ni `down` : ces commandes ouvrent
un micro ou lancent des conteneurs. On verifie leur BRANCHEMENT dans le parseur,
pas leur effet.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from assistant_vocal import AssistantError
from assistant_vocal.cli import (
    _build_parser,
    _lignes_lisibles,
    _lignes_voix,
    _modeles_manquants,
    cmd_down,
    cmd_prompt,
    cmd_run,
    cmd_serve,
    cmd_up,
    main,
)
from assistant_vocal.config import VOIX_PATH, Settings

# ---------------------------------------------------------------------------
# Le parseur
# ---------------------------------------------------------------------------


def test_sans_commande_argparse_refuse():
    """Une sous-commande est obligatoire : `assistant-vocal` seul n'a pas de sens."""
    with pytest.raises(SystemExit) as capture:
        main([])
    assert capture.value.code == 2


def test_version_sort_immediatement(capsys: pytest.CaptureFixture[str]):
    from assistant_vocal import __version__

    with pytest.raises(SystemExit) as capture:
        main(["--version"])
    assert capture.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize(
    ("commande", "attendu"),
    [
        ("run", cmd_run),
        ("serve", cmd_serve),
        ("up", cmd_up),
        ("down", cmd_down),
        ("prompt", cmd_prompt),
    ],
)
def test_chaque_commande_est_branchee(commande: str, attendu: object):
    """On verifie le cablage sans rien executer : c'est tout l'interet de `execute`."""
    args = _build_parser().parse_args([commande])
    assert args.execute is attendu


def test_up_accepte_mode_et_rebuild():
    args = _build_parser().parse_args(["up", "--mode", "natif", "--rebuild"])
    assert args.mode == "natif"
    assert args.rebuild is True


def test_up_refuse_un_mode_inconnu():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["up", "--mode", "cuda"])


# ---------------------------------------------------------------------------
# main() : la traduction des erreurs en code de sortie
# ---------------------------------------------------------------------------


def test_une_erreur_previsible_ne_montre_pas_la_pile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """C'est le contrat de `AssistantError` : un message, pas une trace d'appel."""

    def echoue(_args: object) -> int:
        raise AssistantError("Docker n'est pas demarre")

    monkeypatch.setattr("assistant_vocal.cli.cmd_down", echoue)
    assert main(["down"]) == 1
    erreur = capsys.readouterr().err
    assert "Docker n'est pas demarre" in erreur
    assert "Traceback" not in erreur


def test_ctrl_c_donne_130(monkeypatch: pytest.MonkeyPatch):
    """130 est la convention shell pour « interrompu par SIGINT »."""

    def interrompt(_args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("assistant_vocal.cli.cmd_down", interrompt)
    assert main(["down"]) == 130


def test_une_erreur_inattendue_remonte_avec_sa_pile(monkeypatch: pytest.MonkeyPatch):
    """Tout ce qui n'est pas `AssistantError` doit rester visible."""

    def casse(_args: object) -> int:
        raise ZeroDivisionError("bug")

    monkeypatch.setattr("assistant_vocal.cli.cmd_down", casse)
    with pytest.raises(ZeroDivisionError):
        main(["down"])


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_ne_divulgue_jamais_la_cle_dapi(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Un diagnostic se copie-colle dans une issue : la cle ne doit pas y etre."""
    monkeypatch.setenv("VOICE_API_KEY", "sk-secret-a-ne-pas-fuiter")
    assert main(["doctor"]) == 0
    sortie = capsys.readouterr().out
    assert "sk-secret-a-ne-pas-fuiter" not in sortie
    assert "(definie)" in sortie


def test_doctor_affiche_ses_quatre_sections(capsys: pytest.CaptureFixture[str]):
    assert main(["doctor"]) == 0
    sortie = capsys.readouterr().out
    for section in ("Reglages resolus", "Voix", "Poids attendus", "Ligne d'arguments"):
        assert section in sortie


def test_doctor_verifie_le_modele_regle_pas_celui_en_dur(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """`doctor` mentirait s'il verifiait MODELES_LOCAUX apres un changement de modele.

    La synthese est devenue un REGLAGE avec l'arrivee du clonage : c'est
    `settings.tts_model` qui doit etre verifie en cache, pas la valeur figee.
    """
    monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(tmp_path))
    regle = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit"
    manquants = _modeles_manquants(Settings(tts_model=regle))
    assert regle in manquants
    assert regle in capsys.readouterr().out


def test_doctor_ne_verifie_pas_le_modele_de_langue_en_mode_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """En mode api, aucun poids de modele de langue n'est attendu localement."""
    monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(tmp_path))
    settings = Settings(llm_mode="api", api_base_url="http://x/v1", api_key="k")
    assert settings.llm_model not in _modeles_manquants(settings)


# ---------------------------------------------------------------------------
# Le bloc « Voix » de doctor
# ---------------------------------------------------------------------------


def test_voix_en_clonage_decrit_la_reference():
    lignes = "\n".join(_lignes_voix(Settings()))
    assert "clonage" in lignes
    assert str(VOIX_PATH) in lignes
    assert "sans effet en clonage" in lignes


def test_la_voix_livree_est_un_wav_lisible_par_la_stdlib():
    """Sinon `doctor` degrade en « duree non lue » et perd son interet diagnostique.

    C'est aussi la garantie que le fichier commite est bien un WAV, et pas un
    autre format renomme.
    """
    lignes = "\n".join(_lignes_voix(Settings()))
    assert "duree non lue" not in lignes
    with wave.open(str(VOIX_PATH)) as fichier:
        assert fichier.getnchannels() == 1, "la reference doit etre mono"
        assert fichier.getnframes() > 0


def test_un_audio_illisible_ne_fait_pas_echouer_doctor(tmp_path: Path):
    """La bibliotheque lit d'autres formats que `wave` : c'est une degradation, pas un defaut."""
    faux = tmp_path / "voix.wav"
    faux.write_bytes(b"ce n'est pas du RIFF")
    lignes = "\n".join(
        _lignes_voix(Settings(tts_ref_audio=str(faux), tts_ref_text="une transcription"))
    )
    assert "duree non lue" in lignes


def test_voix_previent_quand_les_emotions_sont_actives_en_clonage():
    """Le piege : `VOICE_EMOTIONS=true` semble marcher, mais le ton n'arrive jamais."""
    lignes = "\n".join(_lignes_voix(Settings(emotions=True)))
    assert "SANS EFFET" in lignes


def test_voix_sans_clonage_annonce_le_locuteur():
    settings = Settings(tts_model="mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit")
    lignes = "\n".join(_lignes_voix(settings))
    assert "locuteur predefini" in lignes
    assert "aiden" in lignes
    assert "clonage" not in lignes


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------


def test_prompt_affiche_le_prompt_du_projet(capsys: pytest.CaptureFixture[str]):
    assert main(["prompt"]) == 0
    assert "assistant vocal francophone" in capsys.readouterr().out


def test_prompt_copy_passe_le_prompt_a_loutil(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    recu: dict[str, object] = {}

    def faux_run(commande: list[str], **kwargs: object) -> object:
        recu["commande"] = commande
        recu["input"] = kwargs.get("input")
        return None

    monkeypatch.setattr("assistant_vocal.cli._outil_presse_papier", lambda: ["pbcopy"])
    monkeypatch.setattr("assistant_vocal.cli.subprocess.run", faux_run)
    assert main(["prompt", "--copy"]) == 0
    assert recu["commande"] == ["pbcopy"]
    assert "assistant vocal francophone" in str(recu["input"])
    assert "Prompt copie" in capsys.readouterr().out


def test_prompt_copy_sans_outil_explique_comment_faire(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Un echec doit toujours donner la manoeuvre de repli."""
    monkeypatch.setattr("assistant_vocal.cli._outil_presse_papier", lambda: None)
    assert main(["prompt", "--copy"]) == 1
    assert "assistant-vocal prompt" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Mise en forme de la ligne d'arguments
# ---------------------------------------------------------------------------


def test_lignes_lisibles_regroupe_drapeau_et_valeur():
    assert _lignes_lisibles(["--a", "1", "--b", "2"]) == ["--a 1", "--b 2"]


def test_lignes_lisibles_garde_un_drapeau_seul():
    assert _lignes_lisibles(["--verbose", "--a", "1"]) == ["--verbose", "--a 1"]


def test_lignes_lisibles_supporte_un_drapeau_final_sans_valeur():
    """Le dernier element n'a pas de suivant : c'est le cas qui deborde le plus facilement."""
    assert _lignes_lisibles(["--a", "1", "--seul"]) == ["--a 1", "--seul"]


def test_lignes_lisibles_tronque_le_prompt_systeme():
    """Le prompt fait plus de mille caracteres : non tronque, le diagnostic devient illisible."""
    (ligne,) = _lignes_lisibles(["--prompt", "x" * 200])
    assert ligne.endswith("...")
    assert len(ligne) <= len("--prompt ") + 60


def test_lignes_lisibles_ecrase_les_retours_a_la_ligne():
    """Une valeur multiligne casserait l'alignement en colonnes de `doctor`."""
    (ligne,) = _lignes_lisibles(["--prompt", "debut\n" + "x" * 200])
    assert "\n" not in ligne
