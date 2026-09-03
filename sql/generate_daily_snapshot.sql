-- =============================================================================
-- generate_daily_snapshot() — snapshot diario de analítica
-- =============================================================================
--
-- Reemplaza a la función que hay hoy en Supabase para que el histórico cuente
-- igual que app/services/analytics.py desde 2026-09-03. Se aplica a mano en el
-- editor SQL de Supabase.
--
-- Lo que cambia respecto a la versión actual, y nada más:
--
--   1. Los contadores de miembros salían de COUNT(*) sobre profiles entero, así
--      que incluían admins e invitados. Ahora todos llevan
--      FILTER (WHERE lower(trim(role)) = 'miembro').
--   2. invited_members comparaba role = 'invitado' con distinción de mayúsculas.
--      El registro por invitación guardaba "Invitado" capitalizado, así que esos
--      perfiles no se contaban en ningún lado: ni como invitados ni como nada.
--      El backend ya normaliza el rol al registrar, pero lower(trim()) protege a
--      las filas viejas.
--   3. non_renewals contaba los expirados de todos los perfiles; ahora solo los
--      de rol miembro.
--
-- Todo lo demás se deja idéntico a propósito: RETURNS void, los nombres
-- calificados con public., el ON CONFLICT, y las cinco subconsultas de pagos.
-- Los ingresos salen de payments, así que no dependen del rol de nadie.
--
-- ⚠️ NO cambies RETURNS void por otra cosa: CREATE OR REPLACE no puede cambiar
-- el tipo de retorno de una función existente y fallaría con "cannot change
-- return type of existing function". Habría que hacer DROP FUNCTION primero, y
-- el job de pg_cron dejaría de existir mientras tanto.
--
-- Esta función NO reescribe las filas ya guardadas: los snapshots anteriores al
-- día en que la apliques conservan los criterios viejos. Solo se recalcula la
-- fila de hoy, cada vez que corre.
--
-- La llaman el job de pg_cron `daily-analytics-snapshot` (3:30 AM UTC) y el
-- endpoint POST /api/admin/analytics/snapshot.
--
-- ---------------------------------------------------------------------------
-- Aparte, sin tocar en este cambio: payments_failed filtra por paid_at::date,
-- pero paid_at solo se escribe al aprobar un pago, así que en un pago fallido
-- queda NULL y ese contador da siempre 0. Si querés que cuente de verdad, el
-- campo es created_at. Se deja como está porque no es parte de lo que se pidió
-- y cambiarlo alteraría cifras de pagos, no de miembros.
-- =============================================================================

CREATE OR REPLACE FUNCTION public.generate_daily_snapshot()
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_date date := current_date;
BEGIN
  INSERT INTO public.analytics_daily_snapshots (
    snapshot_date, total_members, active_members, inactive_members, expired_members,
    invited_members, new_members, revenue_day, revenue_month,
    payments_success, payments_pending, payments_failed, non_renewals
  )
  SELECT v_date,
    -- Miembros: solo role='miembro'. Los admins no se cuentan en ningún lado.
    COUNT(*) FILTER (WHERE lower(trim(role)) = 'miembro'),
    COUNT(*) FILTER (WHERE lower(trim(role)) = 'miembro' AND subscription_status = 'active'),
    COUNT(*) FILTER (WHERE lower(trim(role)) = 'miembro' AND subscription_status = 'inactive'),
    COUNT(*) FILTER (WHERE lower(trim(role)) = 'miembro' AND subscription_status = 'expired'),
    -- Invitados: solo el conteo, aparte de todo lo demás.
    COUNT(*) FILTER (WHERE lower(trim(role)) = 'invitado'),
    COUNT(*) FILTER (WHERE lower(trim(role)) = 'miembro' AND created_at::date = v_date),
    COALESCE((SELECT SUM(amount) FROM public.payments WHERE status = 'success' AND paid_at::date = v_date), 0),
    COALESCE((SELECT SUM(amount) FROM public.payments WHERE status = 'success' AND date_trunc('month', paid_at) = date_trunc('month', now())), 0),
    COALESCE((SELECT COUNT(*) FROM public.payments WHERE status = 'success' AND paid_at::date = v_date), 0),
    COALESCE((SELECT COUNT(*) FROM public.payments WHERE status = 'pending'), 0),
    COALESCE((SELECT COUNT(*) FROM public.payments WHERE status = 'failed' AND paid_at::date = v_date), 0),
    COUNT(*) FILTER (WHERE lower(trim(role)) = 'miembro' AND subscription_status = 'expired')
  FROM public.profiles
  ON CONFLICT (snapshot_date) DO UPDATE SET
    total_members = EXCLUDED.total_members, active_members = EXCLUDED.active_members,
    inactive_members = EXCLUDED.inactive_members, expired_members = EXCLUDED.expired_members,
    invited_members = EXCLUDED.invited_members, new_members = EXCLUDED.new_members,
    revenue_day = EXCLUDED.revenue_day, revenue_month = EXCLUDED.revenue_month,
    payments_success = EXCLUDED.payments_success, payments_pending = EXCLUDED.payments_pending,
    payments_failed = EXCLUDED.payments_failed, non_renewals = EXCLUDED.non_renewals;
END;
$function$;
