-- Run once in the Supabase SQL editor.
-- Bumps reels.updated_at on every UPDATE (upserts included).

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reels_updated_at ON reels;
CREATE TRIGGER trg_reels_updated_at
BEFORE UPDATE ON reels
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
