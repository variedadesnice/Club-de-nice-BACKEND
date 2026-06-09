# CLAUDE.md — Comunyapp Backend

FastAPI + Supabase backend for Comunyapp. Handles auth, posts, courses, payments, invitations, and tags.

## Commands

```bash
uvicorn main:app --reload --port 8000   # Start dev server with hot-reload
pip install -r requirements.txt          # Install dependencies
python -m pytest                         # No test suite yet
```

No test suite exists. Validate logic by running the server and hitting endpoints directly.

## Environment Variables (`.env`)

```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role_key>   # Admin-level access, never expose to clients
PORT=8000
```

`app/core/config.py` validates these at startup via `pydantic-settings`. The `get_settings()` function is `@lru_cache`-d — restart the server if you change `.env`.

---

## Architecture

```
main.py                  # App setup, CORS, exception handlers, router mounting
app/
  core/
    config.py            # Settings (pydantic-settings, lru_cache singleton)
    supabase.py          # Supabase client singletons
    deps.py              # FastAPI auth dependency functions
    exceptions.py        # supabase_error() helper
  api/                   # Route handlers — thin layer, delegates to services
    auth.py, posts.py, courses.py, tags.py, invitations.py, payments.py
  services/              # All business logic lives here
    auth.py, posts.py, courses.py, tags.py, invitations.py, payments.py
  schemas/               # Pydantic request/response models
    auth.py, posts.py, courses.py, tags.py, invitations.py, payments.py
```

**Pattern**: route handler validates input (Pydantic), calls service function, returns result. All DB logic in `services/`. Never put DB queries in `api/`.

### Supabase clients

```python
# app/core/supabase.py
get_supabase()       # Service-role singleton (admin privileges) — use for all DB ops
create_anon_client() # Fresh anonymous client — ONLY for auth.sign_in_with_password()
                     # (avoids contaminating the singleton's session state)
```

The service-role client has `auto_refresh_token=False` and `persist_session=False`. It bypasses Row Level Security — treat it as a direct DB connection with full access.

---

## Database Schema

### `profiles` (extends Supabase `auth.users`)
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | Same ID as `auth.users` |
| `name` | text | Display name |
| `role` | text | `"miembro"` \| `"admin"` \| `"invitado"` |
| `avatar` | text | URL (default: `pravatar.cc/150?u={id}`) |
| `bio` | text | Short bio, nullable |
| `gender` | text | Optional personal field |
| `city` | text | Optional personal field |
| `phone` | text | Optional personal field |
| `subscription_status` | text NOT NULL | `"inactive"` \| `"active"` \| `"expired"` |
| `updated_at` | timestamptz | Set manually in service layer |

> `subscription_status` is maintained by the Postgres trigger `sync_subscription_status` on the `payments` table — do NOT update it manually; approve/reject payments instead.

---

### `posts`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `user_id` | UUID FK → profiles | |
| `content` | text | |
| `image_url` | text | Supabase Storage URL, nullable |
| `pinned` | boolean | Default false |
| `created_at` | timestamptz | Used as pagination cursor |
| `updated_at` | timestamptz | |

### `posts_view` (SQL VIEW)
Read-only view joining `posts` + `profiles` + aggregated tags, likes, and comment counts. **Always query `posts_view` for the feed** — never join manually in code.

### `post_tags` (junction)
`post_id` + `tag_id` (composite PK). Deleted and re-inserted on post edit.

### `tags`
`id`, `name` (unique, stored lowercase), `created_at`

### `post_reactions`
`id`, `post_id`, `user_id`, `reaction_type` (emoji key), `created_at`
Unique constraint on `(post_id, user_id)` — one reaction per user per post.

### `post_comments`
`id`, `post_id`, `user_id`, `content`, `parent_id` (nullable FK → self), `created_at`
Nested replies use `parent_id`. Tree structure is built client-side.

### `comment_reactions`
`id`, `comment_id`, `user_id`, `reaction_type`, `created_at`
Unique constraint on `(comment_id, user_id)`.

---

### `payments`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `user_id` | UUID FK → profiles | |
| `plan` | text | `"1m"` \| `"3m"` \| `"6m"` \| `"1y"` \| `"indefinido"` |
| `amount` | numeric | |
| `status` | text | `"pending"` → `"success"` or `"failed"` |
| `payment_method` | text | Free text description |
| `reference_number` | text | Transaction reference ID |
| `phone` | text | |
| `receipt_url` | text | Path in `receipts` storage bucket |
| `paid_at` | timestamptz | Set on approval |
| `expires_at` | timestamptz | NULL for "indefinido"; calculated from plan on approval |
| `created_at` | timestamptz | |

**Plan durations**: `1m` = 30 d · `3m` = 90 d · `6m` = 180 d · `1y` = 365 d · `indefinido` = NULL

### `invitations`
`id`, `email`, `token` (UUID), `invited_by` (FK → profiles), `expires_at`, `used_at`, `created_at`
Status computed on read: `"pendiente"` / `"usada"` / `"expirada"`.

### `courses`
`id`, `title`, `description`, `thumbnail`, `category` (default "General"), `module` (computed label), `progress` (int), `created_by`, `created_at`

`module` is auto-maintained by `_sync_module_label()` called after chapter changes: `"Sin capítulos"` / `"{title}"` (1 chapter) / `"N capítulos"`.

### `course_chapters`
`id`, `course_id` (FK → courses), `title`, `video_url`, `duration` (text, e.g. `"10:30"`), `sort_order` (int, auto), `created_at`

### Supabase Storage Buckets
| Bucket | Used for |
|--------|----------|
| `Avatars` | Profile pictures + course thumbnails |
| `Post` | Post images |
| `receipts` | Payment receipt files |

---

## Roles & Permissions

| Role | Description | Subscription gating |
|------|-------------|---------------------|
| `miembro` | Default registered user | Required (blocked if inactive/expired) |
| `admin` | Full access to admin panel | Exempt |
| `invitado` | Invited user (pre-registration) | Exempt |

Role is stored in `profiles.role`. The default on registration is `"miembro"`.

### Auth Dependency Chain

```
get_optional_user   →  returns None if no/invalid token (for public endpoints that personalize)
get_current_user    →  returns {id, email} or raises 401
get_current_admin   →  get_current_user + checks role == "admin" or raises 403
get_active_user     →  get_current_user + checks subscription_status == "active"
                        (exempts: admin, invitado)
```

**How token validation works**: each protected request calls `supabase.auth.get_user(token)` with the service-role client to validate the JWT against Supabase Auth. There is no local JWT verification — every call hits Supabase.

### Permission Matrix

| Action | Dependency | Extra check |
|--------|-----------|-------------|
| Read feed / courses / tags | `get_optional_user` | — |
| Create post / comment / react | `get_current_user` | — |
| Delete post | `get_current_user` | author OR admin |
| Edit post | `get_current_user` | — (currently no author check) |
| Manage courses & chapters | `get_current_user` | Frontend admin-only (no server check) |
| Manage tags | `get_current_user` | Frontend admin-only (no server check) |
| All `/api/invitations/` routes | `get_current_admin` | — |
| List / approve / reject payments | `get_current_admin` | — |
| Register + upload receipt | — (public) | Used in onboarding wizard |
| View own payments | `get_current_user` | user_id must match or admin |

> **Gap**: Course and tag management routes use `get_current_user` but the admin check only happens in the frontend. Any authenticated user can technically create/edit/delete courses via the API.

---

## Auth & Registration Flows

### Standard registration (`POST /api/auth/register`)
1. `auth.admin.create_user(email, password, email_confirm=True)` — skips email confirmation
2. Insert `profiles` row (name, role="miembro", avatar=pravatar, bio="", subscription_status="inactive")
3. Auto-login via fresh anon client → returns `{user, token}` or `{autoLogin: false}` on failure

### Payment-wizard registration (`POST /api/payments/register`)
1. `auth.admin.create_user(...)` — same as above
2. Insert `profiles` with `subscription_status="inactive"`
3. Insert `payments` with `status="pending"`
4. Returns `{user, payment, message}` — **no token** (user can't log in until admin reviews)
5. On failure at step 2+: deletes auth user (rollback)

### Login (`POST /api/auth/login`)
1. `anon_client.auth.sign_in_with_password({email, password})`
2. Fetch profile; upsert defaults if missing (name = email prefix, role = "miembro")
3. Returns `{user: {id, name, email, role, avatar, bio, subscription_status, gender, city, phone}, token}`

### User object shape (returned by login / get_me / update_profile)
```json
{
  "id": "uuid",
  "name": "string",
  "email": "string",
  "role": "miembro | admin | invitado",
  "avatar": "url",
  "bio": "string | null",
  "subscription_status": "inactive | active | expired",
  "gender": "string | null",
  "city": "string | null",
  "phone": "string | null"
}
```
> Keep this shape in sync when adding new profile fields: update `get_me`, `login`, and `update_profile` in `app/services/auth.py` together.

---

## Payment & Subscription Flow

```
User registers via wizard
        ↓
payments.status = "pending"
profiles.subscription_status = "inactive"   ← trigger fires on INSERT
        ↓
Admin reviews in panel
        ↓
   approve()                          reject()
        ↓                                  ↓
payments.status = "success"        payments.status = "failed"
payments.expires_at = now + days   subscription_status stays "inactive"
        ↓
Trigger: profiles.subscription_status = "active"
        ↓
User clicks "Actualizar estado" in AccountStatus screen
        → GET /api/auth/me → updateUser() in AuthContext → unblocked
```

The Postgres trigger `sync_subscription_status` on `payments` handles all status transitions. Never update `subscription_status` directly — go through payment approval.

---

## All Endpoints

Auth levels: `—` = public · `🔑` = any authenticated user · `👑` = admin

### Auth (`/api/auth`)
| Method | Path | Auth | Body / Params | Returns |
|--------|------|------|---------------|---------|
| POST | `/api/auth/register` | — | `{name, email, password, role?}` | `{user, token}` or `{autoLogin: false}` |
| POST | `/api/auth/login` | — | `{email, password}` | `{user, token}` |
| GET | `/api/auth/me` | 🔑 | — | `{user}` |
| POST | `/api/auth/avatar` | 🔑 | `{imageData: "data:image/...;base64,..."}` | `{url}` |
| PUT | `/api/auth/profile` | 🔑 | `{name, avatar, bio, gender?, city?, phone?}` | `{user}` |

### Posts (`/api/posts`)
| Method | Path | Auth | Body / Params | Returns |
|--------|------|------|---------------|---------|
| GET | `/api/posts/` | ? | `?limit=10&cursor=<iso>&tags=t1,t2` | `{posts, nextCursor}` |
| POST | `/api/posts/` | 🔑 | `{content, tagIds?, imageData?}` | Post object |
| PATCH | `/api/posts/{post_id}` | 🔑 | `{content?, tagIds?, imageData?, removeImage?}` | `{updated, ...fields}` |
| DELETE | `/api/posts/{post_id}` | 🔑 | — | `{deleted: true}` |
| POST | `/api/posts/{post_id}/pin` | 🔑 | — | `{pinned: bool}` |
| POST | `/api/posts/{post_id}/react` | 🔑 | `{reactionType: string}` | `{reactions: {}, userReaction}` |
| GET | `/api/posts/{post_id}/reactions` | — | — | `[{reaction_type, name, avatar}]` (max 50) |
| GET | `/api/posts/{post_id}/comments` | ? | — | `[Comment]` (tree via parent_id) |
| POST | `/api/posts/{post_id}/comments` | 🔑 | `{content, parentId?}` | Comment object |
| POST | `/api/posts/{post_id}/comments/{comment_id}/react` | 🔑 | `{reactionType}` | `{reactions, userReaction}` |
| GET | `/api/posts/{post_id}/comments/{comment_id}/reactions` | — | — | `[{reaction_type, name, avatar}]` |

**Reaction toggle logic**: same type → remove; different type → replace; no reaction → add.

### Courses (`/api/courses`)
| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| GET | `/api/courses/` | — | — | `[Course]` |
| POST | `/api/courses/` | 🔑 | `{title, description, thumbnail, category?}` | Course |
| POST | `/api/courses/thumbnail` | 🔑 | `{imageData}` | `{url, storage: "supabase"\|"inline"}` |
| PUT | `/api/courses/{course_id}` | 🔑 | `{title?, description?, thumbnail?, category?}` | Course |
| GET | `/api/courses/{course_id}/chapters` | — | — | `[Chapter]` ordered by sort_order |
| POST | `/api/courses/{course_id}/chapters` | 🔑 | `{title, videoUrl?, duration?}` | Chapter |
| PUT | `/api/courses/{course_id}/chapters/{chapter_id}` | 🔑 | `{title?, videoUrl?, duration?}` | Chapter |

### Tags (`/api/tags`)
| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| GET | `/api/tags/` | — | — | `[{id, name}]` sorted A-Z |
| POST | `/api/tags/` | 🔑 | `{name}` | `{id, name}` (idempotent — returns existing if duplicate) |
| DELETE | `/api/tags/{tag_id}` | 🔑 | — | `{deleted: true}` |

### Invitations (`/api/invitations`)
| Method | Path | Auth | Body / Params | Returns |
|--------|------|------|---------------|---------|
| POST | `/api/invitations/` | 👑 | `{email, expiresAt?}` | InvitationOut |
| GET | `/api/invitations/` | 👑 | — | `[InvitationOut]` with computed status |
| DELETE | `/api/invitations/{id}` | 👑 | — | 204 |
| GET | `/api/invitations/validate` | — | `?token=<uuid>` | `{valid, email, reason}` |
| POST | `/api/invitations/use` | — | `{token}` | `{success: bool}` |

### Payments (`/api/payments`)
| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| POST | `/api/payments/upload-receipt` | — | `{reference_number, filename, fileData: "data:...;base64,..."}` | `{path}` |
| POST | `/api/payments/register` | — | `{name, email, password, plan, amount, payment_method, reference_number, phone, receipt_path}` | `{user, payment, message}` |
| GET | `/api/payments/` | 👑 | — | `[Payment]` with user_name, ordered newest first |
| GET | `/api/payments/{user_id}` | 🔑 | — | `[Payment]` for that user |
| PATCH | `/api/payments/{id}/approve` | 👑 | — | Payment (sets status=success, expires_at) |
| PATCH | `/api/payments/{id}/reject` | 👑 | — | Payment (sets status=failed) |
| GET | `/api/payments/{id}/receipt` | 👑 | — | `{url, expires_in: 3600}` |

### Health
| Method | Path | Auth | Returns |
|--------|------|------|---------|
| GET | `/` | — | `{status: "ok", supabase: bool}` |

---

## Key Business Rules

- **Subscription gating**: only `miembro` role is gated. `admin` and `invitado` always have full access.
- **One reaction per user**: unique constraint on `(post_id, user_id)` and `(comment_id, user_id)`.
- **Post delete permission**: user must be author OR admin. Route checks this after fetching the post.
- **Tag deduplication**: stored as lowercase; `create_tag` returns the existing tag if name already exists.
- **Chapter sort_order**: auto-calculated as `existing_count` at insert time. No reorder endpoint.
- **Receipt path sanitization**: chars outside `[a-zA-Z0-9._-]` replaced with `_` to prevent path traversal.
- **Invitation single-use**: `used_at` is set by the RPC `use_invitation(token)` on successful registration.
- **No transaction support**: multi-step operations (register + insert profile + insert payment) use try/except with manual rollback. A partial failure may leave orphaned auth users — check Supabase Auth dashboard if registration seems broken.
- **Module label sync**: always call `_sync_module_label(supabase, course_id)` after inserting/updating/deleting chapters.

---

## Error Handling Patterns

```python
# Standard pattern in services:
try:
    result = supabase.table("...").select("*").execute()
except Exception as exc:
    msg = supabase_error(exc)          # extracts readable message from Supabase exception
    logger.error("[service.fn] FAILED ...", exc_info=True)
    raise HTTPException(status_code=500, detail=msg)
```

`supabase_error(exc)` in `app/core/exceptions.py` extracts the human-readable message from the nested Supabase/PostgREST error structure.

**Custom validation error handler** in `main.py` converts FastAPI's 422 errors to `{"error": str(exc)}`. The error string is Python repr-style (ugly) but parseable.

---

## Logging

Every service function logs with a consistent prefix:
```
[auth.register] step 1/3 - creating auth user
[auth.register] step 2/3 OK
[payments.approve] OK payment_id=<id>
```

Pattern: `[<module>.<function>] <context>`. Use `logger.info` for happy path, `logger.warning` for recoverable issues, `logger.error(..., exc_info=True)` for failures.

---

## Known Gotchas

1. **Trailing slash required on collection endpoints** — routes are defined as `@router.get("/")` under a prefix, making full paths like `/api/posts/`. Missing the slash causes a 307 redirect, and the browser strips the `Authorization` header on cross-origin redirects (confirmed bug).

2. **Service-role client session contamination** — never call `sign_in_with_password` on `get_supabase()`. Use `create_anon_client()` instead. Mixing auth operations into the singleton corrupts its session state for subsequent DB calls.

3. **Supabase token validation is remote** — `supabase.auth.get_user(token)` makes an HTTP call to Supabase on every authenticated request. There is no local JWT caching. High request rates will hit Supabase Auth rate limits.

4. **`posts_view` is the only safe way to paginate** — the cursor uses `created_at`, which must come from the view to include the pinning-aware ordering.

5. **`created_by` on courses is optional** — the column may not exist in all environments. `create_course` retries without it if the first insert fails.

6. **Signed receipt URLs expire in 1 hour** — admin must re-request `GET /api/payments/{id}/receipt` if the URL was cached.

7. **RPC return shape** — `validate_invitation` and other RPCs may return a list `[{...}]` or a dict `{...}`. The `_normalize_rpc()` helper in invitations service handles both.

8. **`subscription_status` NOT NULL constraint** — the `sync_subscription_status` trigger must handle ALL payment status values (`pending`, `success`, `failed`). If a new status is added without updating the trigger, INSERT will fail with a NOT NULL violation.
