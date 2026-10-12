-- Runbook 6cu: "ball lock" for in-store signage. Al: "the zero mercy tour
-- and equinox are attempting to put holes in the ball in the animations"
-- and "can we also not rotate the ball. there are additional logos and
-- rotating it to much will expose those missing on the ball and not look
-- authentic". Prompting Veo wasn't enough (a live test with "UNDRILLED, no
-- finger holes" still grew a row of holes), and pasting the still's ball
-- over Veo's doubled it whenever Veo moved the ball. So with ball_lock on,
-- Veo never sees the ball: Gemini erases it from the still (a "plate"),
-- Veo animates the empty scene, and the renderer composites the ORIGINAL
-- ball back on every frame -- static, real artwork.
--   ball_lock     Al's choice per ball (on by default; off when something
--                 must move across the ball, e.g. Black Widow's spider).
--   raw_is_plate  what the current raw clip actually is -- the renderer
--                 composites the ball only onto a plate animation (a plate
--                 that failed to generate falls back to animating the ball).
alter table article_signage add column if not exists ball_lock boolean not null default true;
alter table article_signage add column if not exists raw_is_plate boolean not null default false;
