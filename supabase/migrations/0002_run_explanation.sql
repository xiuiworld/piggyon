-- The generated explanation for a run, kept beside the run it describes.
--
-- A run is immutable once solved, so its explanation should be too. Generating
-- on read meant the sentences changed between page views of the same plan, and
-- the downloaded verification bundle did not say what the operator had been
-- looking at when they decided. It also put two model calls and six seconds in
-- front of every render.
--
-- Promoted to a column for the same reason `scenarios.validation_result` is:
-- it is written and read as a whole, and never queried into.

alter table runs add column if not exists explanation jsonb;
