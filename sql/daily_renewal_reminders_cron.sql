-- Job de pg_cron que dispara los correos de renovación (daily-renewal-reminders).
--
-- Por qué existe este archivo: el job llamaba al backend con net.http_post sin
-- timeout_milliseconds, y pg_net corta a los 5 s por defecto. El endpoint tarda
-- ~4 s aun sin destinatarios (cold start en Vercel), así que en cualquier día con
-- gente por avisar la petición se cortaba y no salía ningún correo. Entre el
-- 28/09 y el 05/10/2026 debieron enviarse 21 avisos y no salió ninguno.
--
-- Cómo aplicarlo (SQL editor de Supabase):
--   1. Mira el job actual y guarda su definición por si quieres volver atrás:
--        select jobname, schedule, active, command from cron.job
--        where jobname = 'daily-renewal-reminders';
--   2. Ejecuta el cron.schedule de abajo. Con el mismo nombre, pg_cron reemplaza
--      el job existente en vez de crear otro.
--   3. Comprueba que quedó con el timeout:
--        select command from cron.job where jobname = 'daily-renewal-reminders';
--
-- Para ver cómo le fue a una ejecución (net._http_response se borra a las ~6 h):
--   select status_code, timed_out, error_msg, left(content::text, 200), created
--   from net._http_response order by created desc limit 5;

select cron.schedule(
  'daily-renewal-reminders',
  '0 9 * * *',  -- 9:00 UTC todos los días
  $$
  select net.http_post(
    url := (select value from public.app_secrets where key = 'backend_url')
           || '/api/admin/emails/renewal-reminders/cron',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || (select value from public.app_secrets where key = 'service_role_key')
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 60000
  );
  $$
);
