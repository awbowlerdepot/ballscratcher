-- Runbook 6ey: forward drop brush.
--
-- 137 Kegel library patterns carry ForwardDropBrushDistance 9 or 12 with
-- no unit (never printed on the sheet). Read as inches -- Al: "go with
-- inches and bulk approve them" -- and stored here in FEET (9 in -> 0.75)
-- like every other distance. oil_patterns.lanes keeps the buffer brush up
-- on the forward pass until this point (profile model
-- transfer_buffer_carry_v4). Null = brush down from the foul line.

begin;

alter table oil_patterns add column if not exists forward_brush_drop_feet numeric;

create or replace function oil_patterns_bump_content_changed_at() returns trigger
language plpgsql as $$
begin
  if (new.name, new.short_name, new.category, new.series, new.source, new.source_url, new.sheet_url, new.image_url,
      new.status, new.length_feet, new.reverse_brush_drop_feet, new.oil_per_board_ul, new.forward_ml, new.reverse_ml,
      new.volume_ml, new.ratio, new.track_zones, new.machine, new.steps, new.board_totals, new.profile,
      new.profile_source, new.published, new.download_count, new.forward_brush_drop_feet)
     is distinct from
     (old.name, old.short_name, old.category, old.series, old.source, old.source_url, old.sheet_url, old.image_url,
      old.status, old.length_feet, old.reverse_brush_drop_feet, old.oil_per_board_ul, old.forward_ml, old.reverse_ml,
      old.volume_ml, old.ratio, old.track_zones, old.machine, old.steps, old.board_totals, old.profile,
      old.profile_source, old.published, old.download_count, old.forward_brush_drop_feet) then
    new.content_changed_at := now();
  end if;
  new.updated_at := now();
  return new;
end;
$$;

commit;
