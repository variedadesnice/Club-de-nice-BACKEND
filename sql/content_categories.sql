-- Catálogo de categorías de contenido (Classroom + atajos de Comunidad).
--
-- Antes vivían escritas a mano en tres archivos del frontend (PostFeed,
-- Classroom, CreateCourseSheet). courses.category sigue siendo texto: guarda el
-- `name` de una fila de aquí. El backend mantiene los dos alineados: renombrar
-- una categoría renombra también los cursos que la usan, y no deja borrar una
-- categoría con cursos.
--
-- Aplicar en el SQL editor de Supabase ANTES de desplegar el backend y el
-- frontend nuevos (sin la tabla, GET /api/content-categories/ responde 500 y
-- las pantallas se quedan sin categorías). Es idempotente.

create table if not exists public.content_categories (
  id          uuid primary key default gen_random_uuid(),
  name        text not null check (length(trim(name)) > 0),
  description text,
  -- Ícono decorativo del atajo en Comunidad. El frontend tiene un dibujo por
  -- clave; agregar una clave nueva aquí exige agregar su dibujo allá.
  icon        text not null default 'play' check (icon in ('play', 'check', 'doc', 'flask')),
  sort_order  integer not null default 0 check (sort_order >= 0),
  is_active   boolean not null default true,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- "Cursos" y "cursos" serían la misma categoría para quien filtra.
create unique index if not exists content_categories_name_lower_key
  on public.content_categories (lower(name));

-- Mismo criterio que el resto de tablas: sólo el service-role (el backend) entra.
alter table public.content_categories enable row level security;

-- Las cuatro que estaban en el código, con los mismos textos.
insert into public.content_categories (name, description, icon, sort_order) values
  ('Cursos',          'Aprende a tu ritmo con nuestras clases',      'play',  0),
  ('Probando marcas', 'Ponemos a prueba tus ingredientes favoritos', 'check', 1),
  ('Guías digitales', 'Recetas y guías paso a paso',                 'doc',   2),
  ('Laboratorios',    'Probando nuevos trucos en la cocina',         'flask', 3)
on conflict do nothing;

-- Opcional: el único curso actual tiene categoría 'General', que no es ninguna
-- de las cuatro, así que sólo aparece en "Todos". Para moverlo a una real:
--   update public.courses set category = 'Cursos' where category = 'General';
