"""Play-page themes: a look per genre, and a painted backdrop per kind of setting.

Scene tracking tags each scene with one of its theme's setting types, and the play page shows
the matching backdrop (static/backdrops/<theme>/<setting>.webp). The backdrop prompts live here
too, so scripts/make_backdrops.py can regenerate the pack with a local image model.
"""

import re

STYLE = ("Wide cinematic painted backdrop for a {genre} role-playing game. {scene}. The centre "
         "of the image is calmer, with less detail, leaving room for text; the points of interest "
         "sit near the left and right edges. Well lit and clearly visible, with a rich, vibrant "
         "palette of {palette}, soft painterly style, no text, no interface, no people in the "
         "foreground.")
NEGATIVE = ("text, letters, words, readable signs, watermark, logo, user interface, frame, "
            "border, bright busy centre, people in the foreground, close-up faces")

THEMES = {
    "scifi": {
        "label": "Science fiction",
        "genre": "science-fiction",
        "palette": "vivid deep blues and teals with warm amber highlights",
        "matches": ["sci-fi", "science", "space", "traveller", "star", "expanse", "mothership"],
        "settings": {
            "space": "Deep space in transit: a dense but subtle starfield, a faint blue-violet "
                     "nebula along the right edge, a distant freighter with engine glow near the "
                     "left edge",
            "orbit": "High orbit above a gas giant: a large banded ringed planet rising from the "
                     "lower left corner, a small grey moon near the upper right edge, a distant "
                     "boxy freighter with engine glow",
            "planet": "The surface of a rugged alien world at dusk: jagged ridgelines along the "
                      "bottom, a ringed planet low in the sky at the right edge, two small moons "
                      "at the upper left, thin dust haze",
            "settlement": "A starport town at night seen from above: landing pads with lights and "
                          "a parked starship at the lower left, communication masts and habitat "
                          "domes at the right edge, stars above",
            "interior": "Inside a starship corridor: heavy bulkhead ribs and pipes framing the "
                        "left and right edges, a round porthole with stars at the upper right, "
                        "dim amber and blue work lights",
        },
    },
    "fantasy": {
        "label": "Fantasy",
        "genre": "fantasy",
        "palette": "lush greens, golden light and warm earth tones",
        "matches": ["fantasy", "dungeons", "d&d", "pathfinder", "dark eye", "exalted"],
        "settings": {
            "wilds": "Wild northern country at dusk: pine forest and misty hills along the edges, "
                     "a distant ruined watchtower at the left, soft clouds",
            "town": "A medieval market town at twilight: timber houses and a cathedral spire "
                    "along the left edge, lantern-lit rooftops at the right edge, dusky sky",
            "interior": "Inside a great stone hall: carved pillars and hanging banners at the "
                        "edges, a hearth glow at the lower right, shadows in the middle",
            "underground": "A deep dungeon cavern: rough stone arches at the edges, a faint "
                           "torch glow at the lower left, stalactites above, darkness in the "
                           "middle",
            "sea": "A stormy coast at dusk: sea cliffs with a lighthouse on the left edge, a "
                   "sailing ship silhouette at the right, heavy clouds",
        },
    },
    "horror": {
        "label": "Gothic horror",
        "genre": "gothic horror",
        "palette": "moonlit blues, bone white and warm candle gold, moody but clearly visible",
        "matches": ["horror", "gothic", "cthulhu", "vampire", "wraith", "werewolf", "changeling",
                    "heist", "blades"],
        "settings": {
            "interior": "Inside a decaying Victorian manor: dark wooden panelling and a staircase "
                        "at the left edge, a candelabra glow at the right, dust and cobwebs, "
                        "deep shadow in the middle",
            "village": "A fog-bound New England village at night: crooked gabled houses along "
                       "the left edge, a church steeple at the right, a pale full moon behind "
                       "thin cloud",
            "wilds": "A desolate moor at night: twisted bare trees at the edges, standing stones "
                     "on the left, rolling fog, a pale moon",
            "graveyard": "An old overgrown graveyard at night: leaning headstones and a stone "
                         "angel along the edges, iron gates at the right, ground mist, moonlight",
            "underground": "A crypt beneath a church: stone vaults and coffin niches at the "
                           "edges, a single candle glow at the lower left, darkness beyond",
        },
    },
    "cyberpunk": {
        "label": "Cyberpunk",
        "genre": "cyberpunk",
        "palette": "vivid neon magenta, cyan and electric blue against the night",
        "matches": ["cyberpunk", "shadowrun", "cyber", "neon"],
        "settings": {
            "street": "A rain-soaked megacity street at night: towering buildings with glowing "
                      "neon shapes along both edges, puddle reflections, steam from vents",
            "skyline": "A megacity skyline from a rooftop at night: arcologies and flying "
                       "vehicle lights at the edges, rain, dark smoggy sky in the middle",
            "interior": "Inside a cramped neon bar: holographic glow and bottles along the left "
                        "edge, a booth lit magenta at the right, smoky dark middle",
            "corporate": "A sterile corporate tower lobby at night: glass walls and cold white "
                         "light at the edges, city lights far below, dark polished floor",
            "underground": "An abandoned subway tunnel turned slum: cables and makeshift shacks "
                           "at the edges, flickering cyan light at the right, darkness in the "
                           "middle",
        },
    },
}

PLAIN = "plain"  # the original parchment look, no backdrop


def default_for(system: str, genre: str = "") -> str:
    """Pick a theme from the game system (and the picker's genre, if known)."""
    text = f"{system} {genre}".lower()
    for theme_id, t in THEMES.items():
        if any(re.search(rf"(?<![a-z]){re.escape(m)}", text) for m in t["matches"]):
            return theme_id
    return PLAIN


def settings(theme: str) -> list[str]:
    return list(THEMES[theme]["settings"]) if theme in THEMES else []


def prompt(theme: str, setting: str) -> str:
    t = THEMES[theme]
    return STYLE.format(genre=t["genre"], scene=t["settings"][setting], palette=t["palette"])


def public() -> dict:
    """What the play page needs: labels and setting types per theme."""
    return {k: {"label": t["label"], "settings": list(t["settings"])} for k, t in THEMES.items()}
