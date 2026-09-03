-- =============================================================================
-- generate_daily_snapshot() — snapshot diario de analítica
-- =============================================================================
--
-- Alinea el histórico con las reglas que ya aplica app/services/analytics.py
-- desde 2026-09-03:
--
--   * Toda cifra de miembros mira solo role = 'miembro'. Los admins no se
--     cuentan en ningún contador.
--   * De los invitados solo se guarda cuántos hay, en invited_members. No
--     entran en total_members ni en new_members.
--   * La comparación de role es insensible a mayúsculas. La versión anterior
--     comparaba con '=', así que los perfiles guardados como "Invitado" con
--     mayúscula quedaban fuera de invited_members. El backend ya normaliza el
--     rol al registrar, pero lower() protege a las filas viejas.
--
-- ANTES DE APLICARLO: volcá la definición actual y comparala con esta, porque
-- las columnas de pagos (payments_success/pending/failed, revenue_day,
-- revenue_month, non_renewals) se reconstruyeron a partir de los datos que hay
-- en la tabla, no de la función original:
--
--   SELECT pg_get_functiondef(oid)
--   FROM pg_proc
--   WHERE proname = 'generate_daily_snapshot';
--
-- El upsert necesita un índice único sobre snapshot_date. La función anterior ya
-- actualizaba la fila del día, así que debería existir; confirmalo con:
--
--   SELECT indexdef FROM pg_indexes
--   WHERE tablename = 'analytics_daily_snapshots';
--
-- Si no está: CREATE UNIQUE INDEX ON analytics_daily_snapshots (snapshot_date);
--
-- Esta función NO toca las filas ya escritas: los snapshots anteriores a la
-- fecha en que la apliques siguen con los criterios viejos.
--
-- Lo llama el job de pg_cron `daily-analytics-snapshot` (3:30 AM UTC) y el
-- endpoint POST /api/admin/analytics/snapshot.
-- =============================================================================

CREATE OR REPLACE FUNCTION generate_daily_snapshot()
RETURNS analytics_daily_snapshots
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  hoy           date := current_date;
  inicio_mes    date := date_trunc('month', current_date)::date;
  resultado     analytics_daily_snapshots;
BEGIN
  INSERT INTO analytics_daily_snapshots AS s (
    snapshot_date,
    total_members,
    active_members,
    inactive_members,
    expired_members,
    invited_members,
    new_members,
    revenue_day,
    revenue_month,
    payments_success,
    payments_pending,
    payments_failed,
    non_renewals
  )
  SELECT
    hoy,
    -- Miembros: solo role='miembro'. Sin admins, sin invitados.
    (SELECT count(*) FROM profiles
      WHERE lower(trim(role)) = 'miembro'),
    (SELECT count(*) FROM profiles
      WHERE lower(trim(role)) = 'miembro'
        AND lower(trim(subscription_status)) = 'active'),
    (SELECT count(*) FROM profiles
      WHERE lower(trim(role)) = 'miembro'
        AND lower(trim(subscription_status)) = 'inactive'),
    (SELECT count(*) FROM profiles
      WHERE lower(trim(role)) = 'miembro'
        AND lower(trim(subscription_status)) = 'expired'),
    -- Invitados: solo el conteo, aparte de todo lo demás.
    (SELECT count(*) FROM profiles
      WHERE lower(trim(role)) = 'invitado'),
    -- Altas de hoy, también solo miembros.
    (SELECT count(*) FROM profiles
      WHERE lower(trim(role)) = 'miembro'
        AND created_at::date = hoy),
    -- Ingresos: salen de payments, así que no dependen del rol del usuario.
    (SELECT coalesce(sum(amount), 0) FROM payments
      WHERE status = 'success' AND paid_at::date = hoy),
    (SELECT coalesce(sum(amount), 0) FROM payments
      WHERE status = 'success' AND paid_at::date >= inicio_mes),
    (SELECT count(*) FROM payments
      WHERE status = 'success' AND paid_at::date = hoy),
    (SELECT count(*) FROM payments
      WHERE status = 'pending'),
    (SELECT count(*) FROM payments
      WHERE status = 'failed' AND created_at::date = hoy),
    -- No renovaron: miembros cuya suscripción venció y no volvió a activarse.
    (SELECT count(*) FROM profiles
      WHERE lower(trim(role)) = 'miembro'
        AND lower(trim(subscription_status)) = 'expired')
  ON CONFLICT (snapshot_date) DO UPDATE SET
    total_members    = excluded.total_members,
    active_members   = excluded.active_members,
    inactive_members = excluded.inactive_members,
    expired_members  = excluded.expired_members,
    invited_members  = excluded.invited_members,
    new_members      = excluded.new_members,
    revenue_day      = excluded.revenue_day,
    revenue_month    = excluded.revenue_month,
    payments_success = excluded.payments_success,
    payments_pending = excluded.payments_pending,
    payments_failed  = excluded.payments_failed,
    non_renewals     = excluded.non_renewals
  RETURNING s.* INTO resultado;

  RETURN resultado;
END;
$$;
