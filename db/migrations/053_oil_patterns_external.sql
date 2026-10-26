-- Runbook 6et: import the Kegel Pattern Library into oil_patterns.
--
-- Al: "https://patternlibrary.kegel.net/ do you think you can pull in all
-- the patterns from here... KOSI files are there too, which is the format
-- for the lane machines. that might be helpful for future use", then "go
-- ahead, i have already mentioned this and they said they it would be
-- great. clean patterns for this case should be bulk approved."
--
-- external_source/external_id identify a pattern in its publisher's
-- library (Kegel's pattern id) so re-imports update instead of duplicating;
-- external_hash is Kegel's HashSignature, so only changed patterns are
-- rewritten. kosi_key / raw_key point into the private PatternSheetsBucket
-- (the KOSI lane-machine file and Kegel's raw JSON, kept for future use;
-- never published). description is the publisher's blurb (not in the
-- partner contract).

begin;

alter table oil_patterns add column if not exists description     text;
alter table oil_patterns add column if not exists external_source text;
alter table oil_patterns add column if not exists external_id     text;
alter table oil_patterns add column if not exists external_hash   text;
alter table oil_patterns add column if not exists kosi_key        text;
alter table oil_patterns add column if not exists raw_key         text;

create unique index if not exists oil_patterns_external_idx
    on oil_patterns (external_source, external_id) where external_id is not null;

commit;
