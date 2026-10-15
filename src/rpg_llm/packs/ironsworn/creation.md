## Building an Ironsworn character

Walk the player through it a step at a time, recording each choice with update_character as it's
made. Offer to roll on the oracles (ask_oracle) whenever they're unsure.

1. Truths of the Ironlands: ask whether they want to pick the setting's truths, roll them, or keep
   it quick. Quick: roll Truths / The Old World, Iron, Communities and Horrors with ask_oracle and
   tell them in a line each (they may change any). Record each as a short line in "truths".
2. Assets: three, from paths, combat talents, rituals and companions. Look each up with
   rules_lookup and tell them the marked ability (a companion gets a name). Record in asset_cards
   as "Swordmaster: 1".
3. Stats: 3, 2, 2, 1, 1, placed on edge, heart, iron, shadow and wits as they choose.
4. Health, spirit and supply 5, momentum +2, experience 0 (already on the sheet).
5. Background vow: the long-held vow that drives them, rank extreme or epic. Record it as a
   track: {"name": "...", "kind": "vow", "rank": "epic"}. No roll for it.
6. Bonds: if their story has close ties (a home settlement, kin, a mentor), up to three of them:
   one tick each on the Bonds track, and the person or place in companions or the notes.
7. Name, look and manner (Name oracles if they want). Gear: what fits their assets and story;
   no list needed (supply stands for the rest), but note a signature weapon in gear.
8. Inciting incident: offer two or three openings drawn from their vow, bonds and truths (the
   Turning Point and Action/Theme oracles help). When they pick one, call finish_session0; in
   play, the first thing they do about it is Swear an Iron Vow (+heart), a track of the rank you
   agree.
