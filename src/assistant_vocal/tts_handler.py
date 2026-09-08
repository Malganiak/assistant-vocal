"""Sous-classe du handler de synthese : emotions et filtre d'arguments.

Ce module importe le handler de la bibliotheque, donc torch. Il n'est importe
qu'au demarrage, par patches.py -- jamais par config.py ni emotions.py, qui
doivent rester testables sans charger un modele.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from speech_to_speech.TTS.qwen3_tts_handler import Qwen3TTSHandler

from assistant_vocal.emotions import PALETTE, EmotionTracker

logger = logging.getLogger(__name__)

#: Combinaisons (methode, arguments ecartes) deja signalees. Le filtre passe a
#: chaque replique : sans cette memoire, un argument de trop remplirait le
#: journal d'une ligne identique toutes les deux secondes.
_DEJA_SIGNALE: set[tuple[str, tuple[str, ...]]] = set()


def parametres_acceptes(fn: Callable[..., Any]) -> set[str] | None:
    """Noms des parametres acceptes par `fn`, ou None si elle prend `**kwargs`.

    None signifie « accepte tout » : il n'y a alors rien a filtrer.
    """
    parametres = inspect.signature(fn).parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parametres):
        return None
    return {p.name for p in parametres}


def filtrer_sur_signature(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Enveloppe `fn` pour qu'elle ignore les arguments qu'elle n'accepte pas.

    POURQUOI c'est necessaire : les trois methodes de generation de mlx-audio
    n'ont pas la meme signature. Seule `generate()`, le chemin du clonage de
    voix, possede un `**kwargs` et accepte `streaming_context_size` ou `speed`.
    `generate_custom_voice()` et `generate_voice_design()` ont une signature
    FERMEE.

    Ce que coute un argument de trop : un `TypeError` que le `process()` de la
    bibliotheque avale en « Error during Qwen3-TTS generation ». La replique
    est alors perdue **sans un son et sans message utile**. C'est un bug reel,
    rencontre en passant `streaming_context_size` au chemin CustomVoice.
    """
    acceptes = parametres_acceptes(fn)
    if acceptes is None:
        return fn

    @functools.wraps(fn)
    def enveloppe(**kwargs: Any) -> Any:
        ecartes = tuple(sorted(set(kwargs) - acceptes))
        if ecartes:
            cle = (getattr(fn, "__name__", "?"), ecartes)
            # Premiere fois : un avertissement. Ensuite : du debug, pour ne pas
            # noyer le journal.
            niveau = logging.DEBUG if cle in _DEJA_SIGNALE else logging.WARNING
            _DEJA_SIGNALE.add(cle)
            logger.log(
                niveau,
                "%s() n'accepte pas %s : argument(s) ignore(s)",
                cle[0],
                ", ".join(ecartes),
            )
            kwargs = {nom: valeur for nom, valeur in kwargs.items() if nom in acceptes}
        return fn(**kwargs)

    return enveloppe


class Qwen3TTSHandlerAvecEmotions(Qwen3TTSHandler):
    """Handler de synthese dont le ton est choisi par le LLM, replique par replique."""

    #: Renseigne par `patches.install()` depuis les reglages. La bibliotheque
    #: construit ce handler elle-meme, a partir de sa propre dataclasse de
    #: configuration : on ne peut pas lui ajouter un champ. Un attribut de
    #: classe est la façon la plus simple de faire passer ce reglage.
    appliquer_emotions: bool = True

    def setup(self, *args: Any, **kwargs: Any) -> None:
        """Encadre le setup de la bibliotheque.

        Signature ouverte a dessein : la dataclasse de configuration amont
        compte vingt-deux champs, les recopier ici garantirait une divergence
        des la prochaine version.
        """
        # AVANT super() : `setup()` de la bibliotheque se termine par un
        # `warmup()` qui traverse deja `_stream_mlx_generation`. Notre etat doit
        # donc exister a ce moment-la.
        self._tracker = EmotionTracker(appliquer=self.appliquer_emotions)

        super().setup(*args, **kwargs)

        # Rendu a la fin de chaque session, et utilise quand aucune emotion
        # n'est demandee. C'est la valeur venue de `--qwen3_tts_instruct`.
        self._consigne_par_defaut = self.instruct

        # Cette ligne est le seul indice visible que la substitution de classe a
        # pris. Si elle disparait du journal apres une montee de version de la
        # bibliotheque, c'est que la rustine 1 est devenue inoperante.
        logger.info(
            "synthese avec emotions active (%d etiquettes, application=%s)",
            len(PALETTE),
            self.appliquer_emotions,
        )

    def _coalesce_pending_tts_input(self, current_input: Any) -> tuple[str, str | None]:
        """Lit l'etiquette d'emotion sur le texte deja assemble par la bibliotheque.

        POURQUOI ce point d'accroche : c'est la derniere methode traversee avant
        la synthese, et la seule qui voit le texte COMPLET du morceau -- elle
        vide la file d'attente pour recoller les bouts d'une meme replique.
        Lire l'etiquette plus haut, cote LLM, obligerait a rustiner deux
        hierarchies de handlers distinctes.

        CE QUI LE CASSE : un renommage de cette methode, ou un changement de son
        contrat (elle renvoie aujourd'hui un couple texte / code de langue).
        """
        texte, code_langue = super()._coalesce_pending_tts_input(current_input)

        tour = (current_input.turn_id, current_input.turn_revision)
        texte, consigne = self._tracker.process(texte, tour)

        # `self.instruct` est relu par `_process_custom_voice` au moment d'appeler
        # mlx-audio : l'ecrire ici suffit.
        self.instruct = consigne if consigne is not None else self._consigne_par_defaut
        return texte, code_langue

    def _stream_mlx_generation(
        self,
        generation_fn: Callable[..., Any],
        label: str,
        max_tokens: int,
        **generation_kwargs: Any,
    ) -> Any:
        """Filtre les arguments avant de deleguer a la bibliotheque.

        On enveloppe la fonction de generation plutot que de reecrire cette
        methode : le verrou MLX global et la boucle de streaming restent ceux de
        la bibliotheque, on n'en recopie pas une ligne.

        POURQUOI ICI : c'est le point de passage unique des trois chemins de
        generation MLX (clonage de voix, voix predefinie, voix inventee).
        """
        yield from super()._stream_mlx_generation(
            filtrer_sur_signature(generation_fn),
            label,
            max_tokens,
            **generation_kwargs,
        )

    def on_session_end(self) -> None:
        """Nouvelle conversation : on oublie l'emotion et l'avertissement."""
        super().on_session_end()
        self._tracker.reset()
        self.instruct = self._consigne_par_defaut
