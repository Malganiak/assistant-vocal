"""Assistant vocal francophone bati sur `speech-to-speech` de Hugging Face.

Ce paquet ne reimplemente aucun pipeline : il configure et lance la
bibliotheque tierce, en y ajoutant deux choses qu'elle ne sait pas faire.

Ou aller lire quoi :

    config.py       les reglages (.env) et la fabrication de la ligne
                    d'arguments passee a la bibliotheque
    prompt_fr.txt   le prompt systeme, source unique du depot
    emotions.py     la lecture des etiquettes d'emotion du LLM
    tts_handler.py  la sous-classe du handler de synthese
    patches.py      les trois rustines posees dans la bibliotheque, et pourquoi
    launcher.py     le demarrage conjoint du backend et de l'UI en conteneur
    cli.py          les commandes : run, serve, up, down, doctor, prompt
"""

__version__ = "0.1.0"


class AssistantError(Exception):
    """Erreur previsible, a montrer a l'utilisateur sans trace d'appel.

    La CLI attrape cette exception et n'affiche que son message. Tout le reste
    remonte normalement, avec sa pile : un plantage inattendu doit rester
    visible, c'est ce qui permet de le corriger.
    """
