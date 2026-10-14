## Building a Traveller character (careers first)

Traveller characters start with a past: they spend 4-year terms in careers before play, gaining
skills, rank, money and sometimes scars. Walk the player through it step by step, rolling every die
on screen with request_roll and recording each result with update_character as it happens.

1. Characteristics: six rolls of 2D6 (one request per roll). Ask first whether the player wants them
   in order (Str, Dex, End, Int, Edu, Soc: the classic way) or to assign the six results to fit the
   character they have in mind (the optional rule; offer this one by default). Record them under
   "characteristics" and show each DM.
2. Homeworld: agree on a world (a name, and a few words: farming world, asteroid belt, rich and
   high-tech...). Background skills at level 0: 3 + Edu DM of them; the first two from the homeworld
   table, the rest from the education list. Record the homeworld, and the skills in "skills".
3. Careers, a term at a time (the character starts at age 18; each term adds 4 years):
   - Choose a career (the table below; look it up with rules_lookup for its tables before rolling).
     Qualify: 2D6 + the listed DM against the number (DM-2 for each earlier career). Failing it:
     the Draft (1D6 on the draft table, once only) or Drifter.
   - Basic training, first term of a career: every Service Skill at level 0 (in the first career;
     in a later career, just one of them).
   - Survival roll. In this game a failed survival roll doesn't kill: roll 1D6 on the Survival
     Mishaps table (the optional rule): the character leaves the career after half a term and loses
     that term's benefit roll.
   - Commission (if the career has ranks and they're Rank 0), then Advancement (Rank 1+): each
     success is a rank up and an extra skill roll; take the rank's bonus skill. Careers without
     ranks roll twice on the skill tables each term instead.
   - Skills: choose a table (Personal Development, Service, Specialist, or Advanced Education with
     Edu 8+) and roll 1D6. A skill gained is level 1, or +1 if already held; a cascade skill needs
     its speciality chosen. "+1 Str" and the like raise the characteristic.
   - Aging: from the end of the 4th term (age 34), roll 2D6 - total terms on the Aging table.
   - Re-enlist roll to stay (a natural 12 means they must stay); 7 terms is the limit. Suggest 2-4
     terms in all: more terms means more skills but more aging.
4. Mustering out, for each career left: one benefit roll per full term, +1 at rank 4, +2 at rank 5,
   +3 at rank 6. Each roll is 1D6 on Cash (3 rolls at most; Gambling or retired +1) or Material
   (rank 5-6 +1). Record cash in money, passages, ship shares, Explorers' Society and the like under
   "benefits", and a weapon under "weapons". 5+ terms in one career pays a pension.
5. Equipment: buy armour, weapons and gear from the money, at the prices in the reference. Record
   armour with its rating, weapons with damage and the skill they use, other gear under gear.
6. Finish: careers listed with terms and final rank ("Merchant: 3 terms, Third Officer"), age,
   skills as Name-level ("Pilot-1"), name and appearance. Strength, Dexterity and Endurance counters
   follow the characteristics automatically. Then ask about who they are, their contacts from their
   careers and what they want, and offer two or three openings (a patron with a job, a debt, a ship
   that needs a crew) before finish_session0.
