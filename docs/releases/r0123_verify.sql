-- r0123 pre/post-install verification. Read-only and always rolled back.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';

-- Pre-install: expect no row. Post-install: expect exactly one.
SELECT app, name
FROM django_migrations
WHERE app = 'library' AND name = '0099_track_cue_marker_authority';

-- Pre-install: expect no rows. Post-install: expect cue_marker_state jsonb,
-- cue_marker_revision bigint and analysis_requested_at timestamp with time zone.
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND table_name = 'library_track'
  AND column_name IN ('cue_marker_state', 'cue_marker_revision', 'analysis_requested_at')
ORDER BY column_name;

-- Works on both schemas because to_jsonb(record) does not name absent columns.
-- Post-install every Track has state {}, revision 0 unless used since install,
-- and a nullable analysis request. Existing marker values and Ready-to-Air are
-- reported separately below so the operator can compare pre/post evidence.
SELECT
    count(*) AS tracks,
    count(*) FILTER (WHERE row_json ? 'cue_marker_state') AS with_state_column,
    count(*) FILTER (WHERE row_json ? 'cue_marker_revision') AS with_revision_column,
    count(*) FILTER (WHERE row_json ? 'analysis_requested_at') AS with_analysis_request_column,
    count(*) FILTER (WHERE row_json->'cue_marker_state' <> '{}'::jsonb) AS nonempty_state,
    count(*) FILTER (WHERE (row_json->>'cue_marker_revision')::bigint <> 0) AS nonzero_revision,
    count(*) FILTER (WHERE row_json->>'analysis_requested_at' IS NOT NULL) AS analysis_requested
FROM (SELECT to_jsonb(t) AS row_json FROM library_track AS t) AS rows;

SELECT
    count(*) AS tracks,
    count(*) FILTER (WHERE ready2air) AS ready_to_air,
    count(cue_in_seconds) AS cue_in_values,
    count(cue_out_seconds) AS cue_out_values,
    count(next_start_seconds) AS next_start_values,
    count(intro_until_seconds) AS intro_values,
    count(sweep_start_seconds) AS segue_values,
    count(outro_starts_seconds) AS outro_values,
    count(hook_in_seconds) AS hook_in_values,
    count(hook_out_seconds) AS hook_out_values,
    count(artist_id) AS artist_links,
    count(category_id) AS category_links
FROM library_track;

ROLLBACK;
