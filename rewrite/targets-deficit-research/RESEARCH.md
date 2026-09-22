# Calorie deficit / surplus sizing — what the literature says (2026-09-22)

Question (founder): the calculator applies a flat −500 kcal/day for fat loss. Is that
standard? What do the standards actually say by sex, weight, height, age, training status
and goal? Written for the targets PR (#90) and the goal rule in `macro_calculator.apply_goal`.

Where the flat 500 comes from: it is the clinical-obesity default. ACSM 2001 and the
2013 AHA/ACC/TOS guideline prescribe a 500–750 (ACSM: 500–1000) kcal/day deficit for
overweight/obese ADULTS in supervised programs, aiming at 1–2 lb/week and 5–10% loss in
6 months. NICE (UK) uses 600. MyFitnessPal's default is the same 500. None of these were
written for a lean 20-year-old lifter, a small woman, or a 16-year-old; they assume an
adult with substantial fat mass and clinical follow-up.

## 1. The numbers, by dimension

### 1a. Rate of loss (the athlete / physique literature — closest to cued's users)

| Source | Recommendation | Note |
|---|---|---|
| ISSN position stand, diets & body composition (Aragon 2017) | 0.5–1.0% bodyweight/week; "the higher the baseline body fat, the more aggressively the deficit may be imposed"; leaner → slower to keep lean mass | The general rule for trained people |
| Helms 2014 (natural bodybuilding prep) | 0.5–1%/week to maximize muscle retention; the share of loss that is lean mass rises as the deficit grows | Contest prep = already lean |
| Garthe 2011 (elite athletes, RCT) | 0.7%/wk group GAINED 2.1% lean mass while cutting; 1.4%/wk group gained none. Strength went up only in the slow group | The one head-to-head trial |
| Murphy & Koehler 2022 (meta-regression, RT in a deficit) | A deficit of ~500 kcal/day is the point where lean-mass GAINS from resistance training stop; to preserve lean mass during loss "avoid deficits >500 kcal/day" | 500 is the CEILING for lifters, not the default |
| MacroFactor (product convention) | Offers 0.25–1%/wk; frames 0.25–0.5 as "keep all your muscle", 1% as "faster, may lose some" | What evidence-based apps ship |

Converting rate → daily deficit (3500 kcal/lb, the static approximation; Hall 2011 shows
the real curve flattens because expenditure adapts, so any fixed number overstates loss
after the first weeks — which is exactly why cued's biweekly adaptive loop exists):

| Bodyweight | 0.5%/wk | 0.7%/wk | 1.0%/wk |
|---|---|---|---|
| 139 lb | 0.70 lb → 350 kcal/d | 0.97 → 485 | 1.39 → 695 |
| 172 lb | 0.86 → 430 | 1.21 → 605 | 1.72 → 860 |
| 220 lb | 1.10 → 550 | 1.54 → 770 | 2.20 → 1100 |

So "0.5% per week" is roughly 12–13% of maintenance for the founder and ~22% for
Aislinn. A rate rule scales the deficit with size in the direction the ISSN wants (more
fat mass → larger absolute deficit is fine) — but it has to be capped by the limits below.

### 1b. Body fat / bodyweight — the physiological ceiling

- **Alpert 2005**: the fat store can supply at most ~290 kJ (≈69 kcal) per kg of fat mass
  per day, ≈ **31 kcal per lb of fat per day**. A deficit larger than that is paid from
  lean tissue immediately. A 139-lb man at 12% (17 lb fat) caps at ~520 kcal/day; a
  172-lb woman at ~36% (62 lb fat) caps at ~1900. This is why "obese → aggressive is
  OK, lean → slow" is not a taste, it's arithmetic.
- ISSN/Helms: same direction. Leaner users go 0.5%/wk; higher body fat can sit nearer 1%.

### 1c. Sex — no different percentage, but women hit the floors sooner

- No guideline sets a different deficit fraction for women. What differs is the FLOOR:
  women's maintenance is smaller, so the same 500 is a bigger share and lands under the
  energy-availability line faster.
- **IOC RED-S consensus (2018)**: energy availability = (intake − exercise expenditure) /
  fat-free mass. **< 30 kcal/kg FFM/day = low energy availability** (menstrual, bone,
  immune, mood, performance harms); **≥ 45 = optimal**; 30–45 = moderate risk. Applies to
  men too, with effects appearing at slightly lower EA. A 16-year-old at 172 lb / ~36% BF
  has ~50 kg FFM → 30 × 50 = **1500 kcal + training expenditure ≈ 1600/day** is the LEA
  line. Her 1400 target sits under it.
- AHA/ACC/TOS 2013 absolute floors for prescribed diets: 1200–1500 kcal/day women,
  1500–1800 men. NICE: don't routinely use < 800.

### 1d. Age

- **Under 18.** The IOM EER (used in PR #90) includes a growth term; deficits in a growing
  teenager cut into that. Pediatric staged guidance (Barlow 2007 Expert Committee, the
  basis of the AND pediatric guideline): for ages 12–18, BMI 85th–94th percentile →
  **weight maintenance** (let height catch up); ≥ 95th → gradual loss, **never more than
  2 lb/week**, with any faster loss to be evaluated. The AAP 2016 clinical report on
  preventing obesity AND eating disorders in adolescents says to **discourage dieting**
  as such and coach behaviors (meals, sleep, activity, protein) instead of a number. The
  2023 AAP obesity guideline keeps the behavior-first frame (intensive lifestyle
  treatment) and adds medication for ≥ 12 with obesity — it does not prescribe a kcal
  deficit. Net: a teen default is 0 to −10% with the EA floor enforced, and the coach's
  language should be about food quality, protein and training, not "deficit".
- **18–~40.** The athlete literature above applies as written.
- **≥ 65.** Villareal 2017 (NEJM): dieting obese older adults lose lean mass and bone
  even with exercise; combined aerobic + resistance attenuates but does not prevent it.
  Consensus practice: modest deficits (≈ −10–15%, 200–500 kcal), protein ≥ 1.0–1.2
  g/kg, resistance training mandatory, and no aggressive cuts without a reason.

### 1e. Height

No guideline uses height directly. It enters through maintenance (bigger frame → bigger
TDEE) and through the BMI/fat-mass estimate (which sets the Alpert ceiling and the
reference weight for protein). A percentage rule already handles it.

### 1f. Goal

| Goal | Evidence | Typical prescription |
|---|---|---|
| Fat loss, trained/lean | ISSN, Helms, Garthe, Murphy & Koehler | 0.5–0.7%/wk, ≈ 10–20% of TDEE, never > 500 kcal/day if lean mass matters |
| Fat loss, high body fat, adult | ACSM, AHA/ACC/TOS, NICE, Alpert | 500–750 kcal/day (≈ 20–25% TDEE), floors 1200–1500 W / 1500–1800 M |
| Recomp (trained) | Barakat 2020 | maintenance to −100/−200 kcal, protein ≥ 2.2 g/kg, progressive RT — cued's −10% is the aggressive end |
| Lean bulk, novice/intermediate | Iraki 2019 | +10–20% (≈ 0.25–0.5% BW/week gain), protein 1.6–2.2 g/kg |
| Lean bulk, advanced | Iraki 2019 | +5–10%, slower gain ("2 kg/month is excessive") |
| Endurance | (no deficit literature; EA) | maintenance to +5%; the EA floor is the constraint |

## 2. What cued does today vs. the evidence

- `apply_goal`: fat_loss −500 flat; recomp ×0.9; building +250 flat; endurance +150 flat.
  voice.md says fat loss is −400 (prompt/code mismatch).
- −500 is the clinical-obesity number applied to everyone. For the founder it is 18% of
  TDEE (fine); for Aislinn 26% (over the lean-mass ceiling in Murphy & Koehler, under the
  RED-S line, and a "diet" for a 16-year-old); for a 1600-kcal woman it would be 31%.
- +250 is ~9% for the founder (fine) but a flat number again.
- The biweekly adaptive loop (−150/cycle when a cut is flat) is designed to tighten from a
  mild start. Starting hard inverts it.

## 3. Proposed rule (for the founder to pick)

Replace the flat numbers with a percentage of maintenance plus code-computed limits, in
this order:

1. **Base deficit by goal/status**
   - fat loss, adult: **−15% of TDEE** (≈ 0.5%/wk for most bodies; MacroFactor "moderate")
   - fat loss, adult with BMI ≥ 30: −20% (ISSN "higher baseline body fat → more aggressive")
   - fat loss, under 18: **−10%**, or 0 if BMI < 95th percentile (Barlow: maintain, let
     height catch up) — and the coach never uses the word deficit with them
   - fat loss, ≥ 65: −10%
   - recomp: −10% (keep)
   - build, beginner/intermediate: +10%; advanced: +5%; endurance: +5%
2. **Ceilings** (code, applied after 1): deficit ≤ 500 kcal/day when the user trains (Murphy &
   Koehler); deficit ≤ 31 kcal × estimated fat-mass lb (Alpert; fat mass from Deurenberg
   BF% = 1.2·BMI + 0.23·age − 10.8·male − 5.4 when body_fat_pct is null).
3. **Floors** (code): calories ≥ 30 kcal/kg FFM + training expenditure (RED-S line); ≥ 1400
   (existing universal floor; AHA's 1200/1500 are for supervised programs).
4. **User override band** stays ±15%, so someone who wants a harder cut can take it.
5. **Adaptive loop unchanged**: it tightens −150 per cycle when the trend is flat.

Effect on the three live users (maintenance from PR #90's equations):

| User | Today | −15% rule (+ limits) |
|---|---|---|
| Aislinn, 16 F, 1921 maint | 1400 (floor) | −10% teen → 1729 → **1750**; EA floor ≈ 1600 doesn't bind |
| Krla, 20 F, ~1800 maint | 1450 | −15% → 1530 → **1550** |
| Founder, recomp, 2731 | 2450 | **2450** (unchanged) |

## Sources

- ISSN position stand, diets & body composition: https://pubmed.ncbi.nlm.nih.gov/28630601/
- Helms, Aragon, Fitschen 2014: https://pubmed.ncbi.nlm.nih.gov/24864135/
- Garthe et al. 2011: https://pubmed.ncbi.nlm.nih.gov/21558571/
- Murphy & Koehler 2022: https://pubmed.ncbi.nlm.nih.gov/34623696/
- Alpert 2005 (fat-store energy limit): https://pubmed.ncbi.nlm.nih.gov/15615615/
- Hall et al. 2011 (dynamic model, 3500-rule critique): https://pubmed.ncbi.nlm.nih.gov/21872751/
- ACSM position stand (Jakicic 2001): https://www.ovid.com/jnls/acsm-msse/abstract/00005768-200112000-00026
- AHA/ACC/TOS 2013 obesity guideline: https://www.jacc.org/doi/10.1016/j.jacc.2013.11.004
- NICE NG246 physical activity and diet: https://www.nice.org.uk/guidance/ng246/chapter/Physical-activity-and-diet
- IOC RED-S consensus 2018: https://stillmed.olympics.com/media/Documents/Athletes/Medical-Scientific/Consensus-Statements/REDs/IOC-consensus-statement-Relative-Energy-Deficiency-in-Sport-2018.pdf
- Barlow 2007 Expert Committee (pediatric staged goals): https://pubmed.ncbi.nlm.nih.gov/18055651/
- AAP 2016 Preventing Obesity and Eating Disorders in Adolescents: https://pubmed.ncbi.nlm.nih.gov/27550979/
- AAP 2023 obesity CPG: https://publications.aap.org/pediatrics/article/151/2/e2022060640/190443/
- Villareal et al. 2017 NEJM: https://pubmed.ncbi.nlm.nih.gov/28792875/
- Iraki et al. 2019 (off-season): https://pubmed.ncbi.nlm.nih.gov/31247944/
- Barakat et al. 2020 (recomp): https://journals.lww.com/nsca-scj/Fulltext/2020/10000/Body_Recomposition__Can_Trained_Individuals_Build.3.aspx
- MacroFactor rate guidance: https://macrofactor.com/cutting-calculator/
