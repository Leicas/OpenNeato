# Wall registration prototype

`register_grids(reference, moving)` uses walls only (`2` in floor grids, or boolean
masks). It searches all rotations at 10-degree intervals using FFT translation
correlation, then refines the best 36 candidate basins with bounded Powell
optimization. The transform maps moving `(column,row)` pixel centres to reference
`(column,row)` centres. Positive rotation is clockwise when viewed as an image.

Default wall tolerance is 3 pixels. The fit averages the best 85% of moving-wall
similarities, allowing some changed furniture or doors. Acceptance requires a
score of at least 0.70, at least 65% of wall points within 6 pixels, and a margin
of 0.025 over a geometrically different alternative. Alternatives differ by more
than 9 pixels of median displacement. Confidence is a diagnostic quality score,
not a calibrated probability. A symmetric square and two repeated asymmetric
rooms are explicitly rejected in synthetic tests.

The rigid model assumes the same pixel scale. It does not assume common origins,
orientations, or dock coordinates. Unknown physical resolution prevents area
conversion; elastic deformation, a changed floor, severe partial overlap, and
unsearched alternative basins remain limitations. Fit acceptance provides spatial
alignment evidence only, never evidence that suction was active or a room's
cleaning completed.

Read-only evaluation on the 13 retained real runs on 2026-11-14 accepted 13/13
(including the reference self-fit). Wall scores ranged from 0.754 to 1.0. The
weakest fit is November 2, about -95.95 degrees, and needs visual review. November
14 is about -1.02 degrees with translation (5.77,37.34) pixels, score 0.943,
inlier fraction 0.942, and alternative margin 0.252. These are data-dependent
prototype results, not accuracy ground truth. Per-run execution was about 1–2.2s
on the development machine.

Run `python -m unittest scripts.navigation.test_registration` for synthetic
rotation/translation/mobile-obstacle, rotation ambiguity, translation ambiguity,
and insufficient-data cases. Only NumPy and SciPy are required by this module;
no firmware, frontend, live server, or robot changes are made.

## Bounds when combining multiple days

Registration fits moving walls to reference walls in one direction. It does not
verify that moving free space agrees with reference free space; a convincing
partial wall match can coexist with contradictions elsewhere. Trimming unmatched
walls is intentional for registration, but those walls must not automatically be
discarded as mobile objects in a fused map. A confidence score alone cannot
establish that the maps share one unchanged physical floor or pixel scale.

Outside a transformed map's observed footprint, pixels are unknown: abstain from
the fusion vote. Unknown pixels inside its bounding rectangle must also abstain.
A per-pixel support denominator should count distinct observed days, not every
run or the full image rectangle. Keep the amount of observed support alongside
wall/free vote proportions; a single observation cannot establish persistence.

The reference self-fit is a numerical sanity check, not an independent
observation. Mapped runs may reuse a persistent baseline, so different dates do
not guarantee independent measurements. Near-identical maps should be flagged as
correlated rather than interpreted as repeated confirmation of every wall.
Fusion remains an estimate requiring visual review, especially in partial,
repeated-room or low-support areas. A fitted trajectory is evidence of traversal
only; it cannot establish room cleaning completeness or active suction.
