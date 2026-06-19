"""Static AudioSet-class → caption mapping (the universal "sound bank").

The AudioSet ontology's 527 classes are a fixed, general taxonomy — the same
model covers concerts, studio shows, and committee meetings with no per-event
configuration. We curate the subset that is *meaningful to caption* in live
broadcast and map each class to a bracketed caption label, grouping related
classes onto one caption (e.g. several gunfire classes → [GUNFIRE]).

Keys are exact AudioSet display names (the model's ``id2label`` strings). Any
key that does not exist in the loaded model is ignored at runtime, and a
warning is printed — see ``resolve_against(id2label)``.
"""

from typing import Dict, Iterable, Optional, Set

# ── Caption-worthy classes → bracketed caption text ──────────────────────────
# Multiple AudioSet classes may share one caption (right-hand side).
CAPTION_MAP: Dict[str, str] = {
    # Crowd / audience reaction
    "Applause": "APPLAUSE",
    "Clapping": "APPLAUSE",
    "Cheering": "CHEERING",
    "Crowd": "CROWD NOISE",
    "Chatter": "CROWD CHATTER",
    "Hubbub, speech noise, speech babble": "CROWD CHATTER",
    "Laughter": "LAUGHTER",
    "Giggle": "LAUGHTER",
    "Chuckle, chortle": "LAUGHTER",
    "Baby laughter": "LAUGHTER",

    # Music
    "Music": "MUSIC",
    "Musical instrument": "MUSIC",
    "Singing": "SINGING",
    "Choir": "SINGING",
    "Theme music": "MUSIC",
    "Background music": "MUSIC",
    "Jingle (music)": "MUSIC",

    # Vocal non-speech
    "Crying, sobbing": "CRYING",
    "Baby cry, infant cry": "BABY CRYING",
    "Whistling": "WHISTLING",
    "Shout": "SHOUTING",
    "Yell": "SHOUTING",
    "Screaming": "SCREAMING",
    "Cough": "COUGHING",
    "Sneeze": "SNEEZE",
    "Gasp": "GASP",

    # Alerts / signals
    "Telephone bell ringing": "PHONE RINGING",
    "Ringtone": "PHONE RINGING",
    "Telephone": "PHONE RINGING",
    "Doorbell": "DOORBELL",
    "Ding-dong": "DOORBELL",
    "Alarm": "ALARM",
    "Alarm clock": "ALARM",
    "Buzzer": "BUZZER",
    "Smoke detector, smoke alarm": "ALARM",
    "Fire alarm": "FIRE ALARM",
    "Siren": "SIREN",
    "Civil defense siren": "SIREN",
    "Emergency vehicle": "SIREN",
    "Bell": "BELL",
    "Church bell": "BELL TOLLING",

    # Impacts / loud events
    "Explosion": "EXPLOSION",
    "Boom": "BOOM",
    "Eruption": "EXPLOSION",
    "Gunshot, gunfire": "GUNFIRE",
    "Machine gun": "GUNFIRE",
    "Artillery fire": "GUNFIRE",
    "Fusillade": "GUNFIRE",
    "Fireworks": "FIREWORKS",
    "Firecracker": "FIREWORKS",
    "Knock": "KNOCKING",
    "Slam": "DOOR SLAM",
    "Breaking": "GLASS BREAKING",
    "Shatter": "GLASS BREAKING",
    "Glass": "GLASS BREAKING",
    "Bang": "BANG",
    "Thump, thud": "THUD",

    # Weather / ambience
    "Thunder": "THUNDER",
    "Thunderstorm": "THUNDER",
    "Rain": "RAIN",
    "Wind": "WIND",
    "Wind noise (microphone)": "WIND",

    # Animals (occasionally meaningful on location)
    "Dog": "DOG BARKING",
    "Bark": "DOG BARKING",

    # Vehicles
    "Vehicle horn, car horn, honking": "HORN HONKING",
    "Train horn": "TRAIN HORN",
    "Helicopter": "HELICOPTER",
    "Aircraft": "AIRCRAFT",
}

# ── Speech classes — used for arbitration (dialogue belongs to ASR) ──────────
# When one of these dominates a window, we suppress non-speech captions so the
# ASR transcript owns that moment.
SPEECH_LABELS: Set[str] = {
    "Speech",
    "Male speech, man speaking",
    "Female speech, woman speaking",
    "Child speech, kid speaking",
    "Conversation",
    "Narration, monologue",
    "Speech synthesizer",
}

# Captions that legitimately co-occur with speech (background under dialogue),
# so the speech-dominance gate should NOT suppress them.
COEXISTS_WITH_SPEECH: Set[str] = {"MUSIC", "APPLAUSE", "CHEERING", "CROWD NOISE"}


def caption_for(audioset_label: str) -> Optional[str]:
    """Return bracketed caption text for an AudioSet class, or None to ignore."""
    cap = CAPTION_MAP.get(audioset_label)
    return f"[{cap}]" if cap else None


def resolve_against(id2label: Iterable[str]) -> None:
    """Warn about CAPTION_MAP/SPEECH keys absent from the loaded model.

    Guards against AudioSet display-name drift between model checkpoints — a
    typo'd key would otherwise silently never fire.
    """
    available = set(id2label)
    missing = [k for k in (*CAPTION_MAP, *SPEECH_LABELS) if k not in available]
    if missing:
        print(f"[AudioEvents] {len(missing)} mapped class name(s) not in model "
              f"and will never fire: {', '.join(sorted(missing)[:12])}"
              + (" ..." if len(missing) > 12 else ""))
