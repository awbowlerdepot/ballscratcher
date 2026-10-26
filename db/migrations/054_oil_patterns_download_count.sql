-- Runbook 6ex: publish the publisher's download count.
--
-- Drilld asked for a signal of which patterns matter most for ordering its
-- pattern picker; Kegel's library carries a download count per pattern.
-- Al: "yes publish the download count". Filled by
-- scripts/import_kegel_library.py (null for uploaded sheets), served by the
-- partner API as the optional Pattern.download_count.
--
-- The column joins the content trigger so partners syncing
-- /v1/patterns/changes get it: filling it bumps every Kegel row once, and
-- later re-imports that change a count re-send that pattern.

begin;

alter table oil_patterns add column if not exists download_count integer;

create or replace function oil_patterns_bump_content_changed_at() returns trigger
language plpgsql as $$
begin
  if (new.name, new.short_name, new.category, new.series, new.source, new.source_url, new.sheet_url, new.image_url,
      new.status, new.length_feet, new.reverse_brush_drop_feet, new.oil_per_board_ul, new.forward_ml, new.reverse_ml,
      new.volume_ml, new.ratio, new.track_zones, new.machine, new.steps, new.board_totals, new.profile,
      new.profile_source, new.published, new.download_count)
     is distinct from
     (old.name, old.short_name, old.category, old.series, old.source, old.source_url, old.sheet_url, old.image_url,
      old.status, old.length_feet, old.reverse_brush_drop_feet, old.oil_per_board_ul, old.forward_ml, old.reverse_ml,
      old.volume_ml, old.ratio, old.track_zones, old.machine, old.steps, old.board_totals, old.profile,
      old.profile_source, old.published, old.download_count) then
    new.content_changed_at := now();
  end if;
  new.updated_at := now();
  return new;
end;
$$;

commit;
