-- Run this in Supabase SQL Editor to change invoices.amount from integer to decimal.
-- Converts: values >= 100 (stored as cents) -> divide by 100 (e.g. 1181 -> 11.81);
--           values < 100 (already rounded dollars, e.g. 12) -> keep as-is (12.00).
-- Safe to run: no-op if column is already numeric.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'invoices' AND column_name = 'amount'
      AND data_type = 'integer'
  ) THEN
    ALTER TABLE public.invoices
    ALTER COLUMN amount TYPE NUMERIC(12, 2) USING (
      CASE WHEN amount >= 100 THEN amount / 100.0 ELSE amount::numeric END
    );
  END IF;
END $$;
