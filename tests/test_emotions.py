"""Tests de la lecture des etiquettes d'emotion.

Ce module de test ne charge aucun modele : `emotions.py` est volontairement
pur. Il doit tourner en une fraction de seconde.
"""

from __future__ import annotations

import logging

import pytest

from assistant_vocal.emotions import PALETTE, EmotionTracker, split_tag

TOUR_1 = ("t1", 0)
TOUR_2 = ("t2", 0)


# ---------------------------------------------------------------------------
# Lecture de l'etiquette
# ---------------------------------------------------------------------------


def test_etiquette_simple():
    assert split_tag("[joie] Bonjour !") == ("joie", "Bonjour !")


def test_sans_etiquette_le_texte_est_intact():
    assert split_tag("Bonjour, comment vas-tu ?") == (None, "Bonjour, comment vas-tu ?")


def test_espaces_et_parentheses_toleres():
    """Le LLM ne respecte pas toujours la forme exacte demandee."""
    assert split_tag("  [ empathie ]   Je comprends.") == ("empathie", "Je comprends.")
    assert split_tag("(joie) Super !") == ("joie", "Super !")


def test_accents_replies():
    """« [curiosité] » doit retrouver la cle « curiosite » de la palette."""
    assert split_tag("[Curiosité] Tu parles de janvier ?") == (
        "curiosite",
        "Tu parles de janvier ?",
    )


def test_etiquette_hors_palette_retiree_mais_pas_appliquee():
    """Mieux vaut une emotion ignoree qu'un « crochet colere crochet » prononce."""
    etiquette, texte = split_tag("[colere] Bon.")
    assert texte == "Bon."
    assert etiquette == "colere"
    assert etiquette not in PALETTE


def test_pas_de_faux_positif_sur_un_chiffre():
    """« Le tableau [1] montre » ne doit pas etre charcute."""
    assert split_tag("Le tableau [1] montre trois lignes.") == (
        None,
        "Le tableau [1] montre trois lignes.",
    )


def test_etiquette_connue_repetee_est_retiree():
    """Le LLM en met parfois une par phrase."""
    etiquette, texte = split_tag("[joie] Super. [empathie] Mais bon.")
    assert etiquette == "joie"
    assert texte == "Super. Mais bon."


def test_crochet_inconnu_en_milieu_de_texte_est_conserve():
    """Au milieu d'une phrase, un crochet inconnu est probablement du vrai texte."""
    _, texte = split_tag("[joie] Regarde la note [bis] en bas de page.")
    assert texte == "Regarde la note [bis] en bas de page."


def test_chaque_etiquette_de_la_palette_donne_sa_consigne():
    for etiquette, attendu in PALETTE.items():
        tracker = EmotionTracker()
        _, consigne = tracker.process(f"[{etiquette}] Bonjour.", TOUR_1)
        assert consigne == attendu, etiquette


def test_neutre_ne_donne_aucune_consigne():
    """« neutre » est une etiquette valide qui laisse la voix par defaut."""
    assert PALETTE["neutre"] is None
    _, consigne = EmotionTracker().process("[neutre] Bonjour.", TOUR_1)
    assert consigne is None


# ---------------------------------------------------------------------------
# Memoire d'un tour de parole
# ---------------------------------------------------------------------------


def test_la_consigne_est_gardee_sur_toute_la_replique():
    """Une replique arrive en morceaux, seul le premier porte l'etiquette."""
    tracker = EmotionTracker()

    texte, consigne = tracker.process("[joie] Ah, c'est regle !", TOUR_1)
    assert (texte, consigne) == ("Ah, c'est regle !", PALETTE["joie"])

    texte, consigne = tracker.process("Tout est reparti.", TOUR_1)
    assert (texte, consigne) == ("Tout est reparti.", PALETTE["joie"])


def test_la_consigne_est_oubliee_au_changement_de_tour():
    tracker = EmotionTracker()
    tracker.process("[joie] Ah !", TOUR_1)

    _, consigne = tracker.process("Bonjour.", TOUR_2)
    assert consigne is None, "l'emotion d'un tour ne doit pas fuir sur le suivant"


def test_une_revision_de_tour_est_un_nouveau_tour():
    """La bibliotheque incremente la revision quand l'utilisateur reprend la parole."""
    tracker = EmotionTracker()
    tracker.process("[joie] Ah !", ("t1", 0))

    _, consigne = tracker.process("Bonjour.", ("t1", 1))
    assert consigne is None


def test_emotions_desactivees_retirent_quand_meme_letiquette():
    """Sans emotions, l'etiquette ne doit surtout pas etre prononcee."""
    tracker = EmotionTracker(appliquer=False)
    texte, consigne = tracker.process("[joie] Bonjour.", TOUR_1)
    assert texte == "Bonjour."
    assert consigne is None


# ---------------------------------------------------------------------------
# L'avertissement, sentinelle des rustines
# ---------------------------------------------------------------------------


def test_avertissement_une_seule_fois_par_session(caplog: pytest.LogCaptureFixture):
    """C'est le seul mecanisme capable de detecter une rustine inoperante.

    Il doit se declencher, mais une seule fois : sinon il devient du bruit que
    l'on apprend a ignorer.
    """
    tracker = EmotionTracker()
    with caplog.at_level(logging.WARNING, logger="assistant_vocal.emotions"):
        tracker.process("Bonjour.", TOUR_1)
        tracker.process("Bonsoir.", TOUR_2)
        tracker.process("Bonne nuit.", ("t3", 0))

    avertissements = [e for e in caplog.records if "aucune etiquette" in e.message]
    assert len(avertissements) == 1


def test_pas_davertissement_si_les_etiquettes_arrivent(caplog: pytest.LogCaptureFixture):
    tracker = EmotionTracker()
    with caplog.at_level(logging.WARNING, logger="assistant_vocal.emotions"):
        tracker.process("[joie] Bonjour.", TOUR_1)
        tracker.process("[calme] Bonsoir.", TOUR_2)

    assert not [e for e in caplog.records if "aucune etiquette" in e.message]


def test_reset_rearme_lavertissement(caplog: pytest.LogCaptureFixture):
    tracker = EmotionTracker()
    with caplog.at_level(logging.WARNING, logger="assistant_vocal.emotions"):
        tracker.process("Bonjour.", TOUR_1)
        tracker.reset()
        tracker.process("Bonjour.", TOUR_1)

    avertissements = [e for e in caplog.records if "aucune etiquette" in e.message]
    assert len(avertissements) == 2


# ---------------------------------------------------------------------------
# Cas limite
# ---------------------------------------------------------------------------


def test_replique_reduite_a_une_etiquette(caplog: pytest.LogCaptureFixture):
    """Sans garde-fou, la bibliotheque ferait dire « Hello. » en anglais.

    Son `process()` remplace un texte vide par cette chaine codee en dur. On
    renvoie donc un espace, qui produit un silence.
    """
    tracker = EmotionTracker()
    with caplog.at_level(logging.WARNING, logger="assistant_vocal.emotions"):
        texte, consigne = tracker.process("[joie]", TOUR_1)

    assert texte == " "
    assert consigne == PALETTE["joie"]
    assert [e for e in caplog.records if "vide apres retrait" in e.message]


def test_le_prompt_documente_toute_la_palette():
    """Le prompt et le code ne doivent pas pouvoir diverger.

    Si on ajoute une etiquette a la palette sans l'ecrire dans le prompt, le LLM
    ne l'emettra jamais : le code aurait une branche morte.
    """
    from assistant_vocal.config import read_prompt

    prompt = read_prompt()
    for etiquette in PALETTE:
        assert f"[{etiquette}]" in prompt, f"{etiquette} absent de prompt_fr.txt"
