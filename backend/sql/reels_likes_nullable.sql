-- Run in the Supabase SQL editor.
-- likes may be NULL when Instagram hides like/view counts
-- (like_and_view_counts_disabled) — GraphQL like_count is unreliable then.

ALTER TABLE reels
  ALTER COLUMN likes DROP NOT NULL;

-- Optional: clear existing stub values you no longer trust.
-- Uncomment if you want to null-out rows that still have the old hidden-count stubs:
-- UPDATE reels SET likes = NULL WHERE likes = 3;
