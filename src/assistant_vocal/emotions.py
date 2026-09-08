"""Emotions pilotees par le LLM.

Le LLM prefixe chaque replique d'une etiquette entre crochets. Ce module la
lit, la traduit en consigne de ton pour Qwen3-TTS, et la retire du texte pour
qu'elle ne soit pas prononcee.

Ce module est PUR : il n'importe ni `speech_to_speech`, ni torch, ni mlx. La
sous-classe qui l'utilise vit dans tts_handler.py. C'est ce decoupage qui rend
ces tests instantanes.

Pourquoi des crochets, et pourquoi ca survit au pipeline : `[` et `]` font
partie des caracteres que la bibliotheque considere comme prononcables, donc
son nettoyage `remove_unspeechable` les laisse passer ; `remove_markdown` ne
touche ni aux crochets ni aux debuts de ligne hors titres et puces ; et la
decoupe en phrases garde l'etiquette collee a la premiere phrase, puisqu'il n'y
a pas de ponctuation finale apres le crochet fermant. Verifie sur la version
1.0.0.
"""

from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

#: Etiquette -> consigne de ton envoyee au modele de synthese.
#:
#: Les consignes sont en ANGLAIS a dessein : le modele repond aux deux langues,
#: mais les formulations anglaises donnent un contraste d'energie plus net. Le
#: texte synthetise, lui, reste evidemment francais.
#:
#: La palette est volontairement etroite et conversationnelle. Mesure sur quatre
#: tirages de la meme replique : le neutre tient 5,2 s, « Sad, slow and quiet. »
#: monte a 7,3 s (+40 %) avec un ecart-type de 2,1 s. Les emotions fortes
#: etirent le debit et deviennent instables -- elles n'ont pas leur place dans
#: un echange qui doit rester fluide. D'ou l'absence de tristesse et de colere.
PALETTE: dict[str, str | None] = {
    "neutre": None,  # etiquette valide, aucune consigne : la voix par defaut
    "calme": "Calm and reassuring.",
    "chaleur": "Warm and friendly.",
    "joie": "Happy and bright.",
    "enthousiasme": "Enthusiastic and energetic.",
    "amusement": "Amused, with a smile in the voice.",
    "curiosite": "Curious and interested.",
    "empathie": "Gentle and empathetic.",
    "desole": "Apologetic and soft.",
}

#: Etiquette en TETE de replique : crochets ou parentheses, espaces tolerés,
#: lettres accentuees admises (le LLM ecrit parfois « [curiosité] »).
#: Volontairement sans chiffres, pour ne pas manger un « [1] » du texte.
_TAG_EN_TETE = re.compile(r"^\s*[\[(]\s*([^\W\d_]{2,20}(?:[_-][^\W\d_]{2,20})?)\s*[\])]\s*")

#: La meme chose, n'importe ou dans le texte. Sert a rattraper le LLM qui
#: etiquette chaque phrase. On ne retire ces occurrences que si l'etiquette est
#: CONNUE : un crochet inconnu au milieu d'une phrase a plus de chances d'etre
#: du vrai texte qu'une etiquette.
_TAG_PARTOUT = re.compile(r"[\[(]\s*([^\W\d_]{2,20}(?:[_-][^\W\d_]{2,20})?)\s*[\])]")


def _replier(etiquette: str) -> str:
    """« Curiosité » -> « curiosite », pour tolerer les accents du LLM."""
    decompose = unicodedata.normalize("NFD", etiquette.strip().lower())
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


def split_tag(text: str) -> tuple[str | None, str]:
    """Separe l'etiquette de tete du reste du texte.

    Renvoie `(etiquette repliee ou None, texte sans etiquette)`.

    L'etiquette de tete est retiree meme si elle est inconnue : mieux vaut une
    emotion ignoree qu'un « crochet colere crochet » lu a voix haute. Les
    etiquettes CONNUES situees plus loin dans le texte sont retirees aussi,
    parce que le LLM en met parfois une par phrase. Les crochets inconnus au
    milieu du texte sont laisses tels quels : c'est probablement du vrai texte.
    """
    etiquette: str | None = None

    correspondance = _TAG_EN_TETE.match(text)
    if correspondance is not None:
        etiquette = _replier(correspondance.group(1))
        text = text[correspondance.end() :]

    def _retirer_si_connue(m: re.Match[str]) -> str:
        return " " if _replier(m.group(1)) in PALETTE else m.group(0)

    text = _TAG_PARTOUT.sub(_retirer_si_connue, text)
    return etiquette, re.sub(r"\s{2,}", " ", text).strip()


class EmotionTracker:
    """Retient l'emotion d'un tour de parole, et rale si on n'en voit jamais.

    Pourquoi une memoire : une replique arrive en plusieurs morceaux et seul le
    premier porte l'etiquette. La cle est `(turn_id, turn_revision)` -- la
    bibliotheque incremente `turn_revision` quand l'utilisateur reprend la
    parole sur un tour encore ouvert, et c'est alors une nouvelle replique,
    donc potentiellement une nouvelle emotion.
    """

    def __init__(self, *, appliquer: bool = True) -> None:
        self._appliquer = appliquer
        self._tour: tuple[object, object] | None = None
        self._consigne: str | None = None
        self._deja_prevenu = False

    def process(self, text: str, tour: tuple[object, object]) -> tuple[str, str | None]:
        """Traite un morceau de replique.

        Renvoie `(texte a prononcer, consigne de ton ou None)`.
        """
        nouveau_tour = tour != self._tour
        if nouveau_tour:
            self._tour = tour
            self._consigne = None

        etiquette, propre = split_tag(text)

        if etiquette is not None and etiquette in PALETTE:
            self._consigne = PALETTE[etiquette] if self._appliquer else None
            logger.info("emotion %r -> consigne %r", etiquette, self._consigne)
        elif etiquette is not None:
            # Le LLM a invente un mot. On l'a deja retire du texte ; on ne
            # change simplement pas le ton.
            logger.debug("etiquette hors palette, ignoree : %r", etiquette)
        elif nouveau_tour:
            self._avertir_si_besoin()

        if not propre:
            # Sans ce garde-fou, la bibliotheque remplace un texte vide par
            # « Hello. » et l'assistant prononce un mot anglais surgi de nulle
            # part. Un espace la laisse produire un silence.
            logger.warning("replique vide apres retrait de l'etiquette : %r", text)
            propre = " "

        return propre, self._consigne

    def reset(self) -> None:
        """Fin de session : on oublie tout, y compris l'avertissement."""
        self._tour = None
        self._consigne = None
        self._deja_prevenu = False

    def _avertir_si_besoin(self) -> None:
        """Previent UNE FOIS par session que les etiquettes n'arrivent pas.

        C'est le seul mecanisme du projet capable de detecter qu'une rustine a
        cesse de s'appliquer : si le prompt du projet est ecrase, ou si la
        substitution de classe est devenue inoperante, le symptome visible est
        precisement l'absence d'etiquettes.
        """
        if self._deja_prevenu:
            return
        self._deja_prevenu = True
        logger.warning(
            "aucune etiquette d'emotion dans la reponse du LLM. Le prompt du projet "
            "est-il bien actif ? Voir la section « Emotions » du README."
        )
