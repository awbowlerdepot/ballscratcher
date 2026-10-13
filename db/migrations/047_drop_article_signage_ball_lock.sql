-- Runbook 6cw: the 6cu "ball lock" is removed. Al: "locking that pixel for
-- pixel is not working. the window spare+ is a prime example. the spider
-- was animated on the fixed ball before now it is horribly cropped and
-- randomly moving with the spider from the image. remove the restriction
-- for rotating the ball but keep the hole restriction". Veo animates the
-- whole scene again; nothing reads these columns any more. Run AFTER the
-- code deploy (the previous admin API selects them).
alter table article_signage drop column if exists ball_lock;
alter table article_signage drop column if exists raw_is_plate;
