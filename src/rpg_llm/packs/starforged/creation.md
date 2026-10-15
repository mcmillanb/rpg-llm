## Building a Starforged character

Starforged characters are defined by paths (assets), a starship and a vow. Walk the player
through it a step at a time, recording each choice with update_character as it's made. Offer to
roll on the oracles (ask_oracle) whenever they're unsure.

1. Truths of the Forge: ask whether they want to pick the setting's truths, roll them, or keep it
   quick. Quick: roll Truths / Cataclysm, Exodus, Communities, Iron and Precursors with ask_oracle
   and tell them in a line each (they may change any). Record each as a short line in "truths".
2. Paths: two path assets (the Backgrounds table gives a pair if they want one). Look each up
   with rules_lookup and tell them the marked ability. Record in asset_cards as "Ace: 1".
3. Backstory: a few sentences (Campaign Launch Oracles / Backstory Prompts if they want ideas).
4. Background vow: a vow that comes out of the backstory, rank extreme or epic. Record it as a
   track: {"name": "...", "kind": "vow", "rank": "epic"}. No roll for it.
5. Starship: everyone has the Starship command vehicle asset ("Starship: 1"): a name, its history
   and a quirk (Starship History, Starship Quirks oracles). Record in starship; integrity 5.
6. One more asset: a module, a support vehicle, a companion or a third path (not a deed).
7. Stats: 3, 2, 2, 1, 1, placed on edge, heart, iron, shadow and wits as they choose.
8. Health, spirit and supply 5, momentum +2; experience 0; the legacy tracks start empty (the app
   puts the meters and legacy tracks on the sheet already).
9. Name, pronouns and callsign, look and manner (Character Oracles / Character Name, First Look).
   No gear list: they carry what their paths and background suggest; supply stands for the rest.
10. Starting point, kept light: roll a starting settlement's name and its trouble (Settlement
    Oracles, Campaign Launch Oracles / Sector Trouble) and mention a couple of places nearby.
11. Inciting incident: offer two or three (Campaign Launch Oracles / Inciting Incident, or a
    truth's quest starter). When they pick one, call finish_session0; in play, the first thing
    they do about it is Swear an Iron Vow (+heart), a track of the rank you agree.
