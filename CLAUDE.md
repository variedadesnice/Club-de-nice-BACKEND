# CLAUDE.md — El Club de Nice Backend

FastAPI + Supabase backend for El Club de Nice. Handles auth, social feed, classroom/courses (with PDFs), live streaming (chat/reactions/PDFs over WebSocket), gamification (levels/achievements/streaks), multi-currency payments, configurable payment methods, invitations, tags, and admin analytics.

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
REDIS_URL=redis://...                          # Optional — caching + rate limiting (degrades gracefully if unset)
GEMINI_API_KEY=...                             # Unused leftover, no service references it
```

`app/core/config.py` validates these at startup via `pydantic-settings`. The `get_settings()` function is `@lru_cache`-d — restart the server if you change `.env`.

The backend does **not** fetch BCV exchange rates itself — the frontend fetches the rate and sends a frozen snapshot at payment time (see Multi-Currency Payments below).

---

## Architecture

```
main.py                  # App setup, CORS, exception handlers, router mounting
app/
  core/
    config.py            # Settings (pydantic-settings, lru_cache singleton)
    supabase.py           # Supabase client singletons
    redis_client.py       # Redis singleton (returns None if REDIS_URL unset/unreachable)
    cache.py              # cache_get/cache_set/cache_delete[_pattern] — JSON, no-op without Redis
    rate_limit.py          # rate_limiter(max, window_s, prefix) FastAPI dependency, fixed-window via Redis
    ws_manager.py          # ConnectionManager singleton — broadcasts WS messages per live_id (Lives chat)
    deps.py               # FastAPI auth dependency functions
    exceptions.py         # supabase_error() helper
  api/                    # Route handlers — thin layer, delegates to services
    auth.py, posts.py, tags.py, invitations.py, payments.py
    courses.py             # LEGACY course/chapter CRUD — still mounted, still used by frontend admin UI
    classroom.py           # NEW student-facing: progress, completion, chapter PDFs (read)
    admin_classroom.py     # NEW admin: course publish toggle, chapter delete, chapter PDF management
    levels.py, achievements.py, admin_gamification.py (admin_levels_router + admin_achievements_router)
    lives.py, admin_lives.py
    currencies.py, admin_currencies.py
    payment_methods.py, admin_payment_methods.py
    analytics.py           # Admin dashboard stats
    streaks.py             # Daily check-in / streak tracking
  services/               # All business logic lives here (mirrors app/api/ module names)
  schemas/                # Pydantic request/response models (mirrors app/api/ module names)
```

**Pattern**: route handler validates input (Pydantic), calls service function, returns result. All DB logic in `services/`. Never put DB queries in `api/`.

### Router mount order (`app/api/__init__.py`)

```
/api/auth  /api/posts  /api/courses  /api/tags  /api/invitations  /api/payments
/api/payment-methods  /api/admin/payment-methods
/api/levels  /api/achievements  /api/admin/levels  /api/admin/achievements
/api/admin/analytics
/api/classroom  /api/admin/classroom
/api/lives  /api/admin/lives
/api/currencies  /api/admin/currencies
/api/streaks
```

Health endpoint: `GET /` → `{status: "ok", supabase: bool, redis: bool}`.

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
| `avatar` | text | URL, empty string for new users (frontend shows the first letter of the name as fallback) |
| `bio` | text | Short bio, nullable |
| `gender` | text | Optional personal field |
| `city` | text | Optional personal field |
| `phone` | text | Optional personal field |
| `birthdate` | date | Optional personal field. Backs the `v_stats_ages` analytics view (age-range breakdown) — keep populated if you want that report meaningful |
| `subscription_status` | text NOT NULL | `"inactive"` \| `"active"` \| `"expired"` |
| `updated_at` | timestamptz | Set manually in service layer |

> `subscription_status` is maintained by the Postgres trigger `sync_subscription_status` on the `payments` table — do NOT update it manually; approve/reject payments instead.

### `posts` / `posts_view` / `post_tags` / `tags` / `post_reactions` / `post_comments` / `comment_reactions`
Unchanged from the original social-feed design. **Always query `posts_view` for the feed** — never join manually in code. New: `GET /api/posts/me/social-impact` (🔑) returns an aggregate impact score for the current user (used in Profile stats).

---

### `payments` (multi-currency)
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `user_id` | UUID FK → profiles | |
| `plan` | text | `"1m"` \| `"3m"` \| `"6m"` \| `"1y"` \| `"indefinido"` |
| `amount` | numeric | USD amount, base currency for internal reporting |
| `amount_local` | numeric | Amount in the local currency the user actually paid (e.g. Bs) |
| `currency_id` | UUID FK → currencies | Currency the user paid in |
| `exchange_rate` | numeric | Frozen rate at payment time (1 USD = X local currency) — never recalculated later |
| `payment_method_id` | UUID FK → payment_methods | Which configured method the user selected |
| `status` | text | `"pending"` → `"success"` or `"failed"` |
| `reference_number` | text | Transaction reference ID |
| `phone` | text | |
| `receipt_url` | text | Path in `receipts` storage bucket |
| `paid_at` | timestamptz | Set on approval |
| `expires_at` | timestamptz | NULL for "indefinido"; calculated from plan on approval |
| `created_at` | timestamptz | |

**Plan durations**: `1m` = 30 d · `3m` = 90 d · `6m` = 180 d · `1y` = 365 d · `indefinido` = NULL

### `currencies`
`id`, `code` (unique, normalized uppercase), `name`, `symbol`, `is_base` (bool — the USD/base row, can't be deleted or deactivated), `is_active`, `created_at`, `updated_at`

### `payment_methods` / `payment_method_fields` / `payment_method_values`
Configurable catalog of payment instructions admins manage, surfaced to users during registration:
- `payment_methods`: `id`, `name`, `description`, `is_active`, `sort_order`
- `payment_method_fields`: `id`, `payment_method_id` FK, `field_key`, `field_label`, `field_type` (`text`\|`email`\|`phone`\|`number`), `is_required`, `sort_order`
- `payment_method_values`: `id`, `payment_method_id` FK, `payment_method_field_id` FK, `value` (nullable) — the actual displayed value (e.g. an account number)

The user just sends `payment_method_id` + `reference_number` at registration; the fields/values are for display/copy-to-clipboard guidance only.

### `invitations`
`id`, `email`, `token` (UUID), `invited_by` (FK → profiles), `expires_at`, `used_at`, `created_at`
Status computed on read: `"pendiente"` / `"usada"` / `"expirada"`.

### `courses` / `course_chapters` (legacy CRUD, still primary path)
`courses`: `id`, `title`, `description`, `thumbnail`, `category` (default "General"), `module` (computed label), `progress` (int, static field — NOT wired to the new classroom progress-tracking endpoints), `created_by`, `created_at`
`course_chapters`: `id`, `course_id` FK, `title` (max 150 chars), `video_url`, `duration` (text, max 20 chars, e.g. `"10:30"`), `sort_order` (int, auto), `created_at`

`module` is auto-maintained by `_sync_module_label()` called after chapter changes: `"Sin capítulos"` / `"{title}"` (1 chapter) / `"N capítulos"`.

### `chapter_pdfs`
`id`, `chapter_id` FK → course_chapters, `title`, `file_url`, `sort_order`, `created_at`. Stored in bucket `chapter-pdfs` at `{chapter_id}/{timestamp}_{safe_filename}`.

### `user_course_progress` (used by the newer `/api/classroom` student endpoints)
Tracks per-user chapter completion. Backs `complete_chapter` / `get_course_progress` / `get_completed_courses_count`. **Not yet wired into the frontend UI** — `CourseDetail.tsx` only reads the static `courses.progress` column, not this table.

---

### Lives (live streaming)

| Table | Columns |
|-------|---------|
| `live_sessions` | `id`, `title`, `description`, `youtube_url`, `is_active`, `scheduled_at`, `ended_at`, `created_by`, `created_at` |
| `live_chat_messages` | `id`, `live_id`, `user_id`, `content`, `created_at`, `edited_at`, `is_pinned` |
| `live_reactions` | `id`, `live_id`, `user_id`, `reaction_type`, `created_at` — unique `(live_id, user_id)` |
| `live_pdfs` | `id`, `live_id`, `title`, `file_url`, `sort_order`, `created_at` — bucket `live-pdfs` |

**WebSocket**: `app/core/ws_manager.py` exposes a global `manager` (`ConnectionManager`) keyed by `live_id`. `GET /api/lives/{live_id}/chat/ws?token=<jwt>` validates the token best-effort (read-only socket, so it accepts the connection even if validation fails) and broadcasts `new_message` / `reaction_update` / `edit_message` / `delete_message` / `pin_message` events to all connections on that `live_id`.

**Caching**: the lives list (`GET /api/lives/`) is cached in Redis under `lives:all` for 5 seconds — short TTL so start/end state shows up quickly — and invalidated on any admin create/update/activate/delete.

---

### Gamification

| Table | Columns |
|-------|---------|
| `level_tiers` | `id`, `name`, `min_level`, `max_level`, `description`, `icon_url` — bucket `level-tier-icons` |
| `achievement_types` | `id`, `code` (unique), `name`, `description`, `xp_reward`, `is_repeatable`, `daily_limit`, `icon_url` (bucket `achievement-icons`), `is_active` |
| `user_levels` | `user_id` PK, `level`, `xp_total`, `xp_current`, `xp_next`, `updated_at` |
| `user_achievements` | `id`, `user_id` FK, `achievement_type_id` FK, `obtained_at`, `metadata` JSON |
| `xp_transactions` | `id`, `user_id` FK, `amount`, `reason`, `achievement_type_id` FK, `created_at` |

RPC `award_xp(p_user_id, p_amount, p_reason, p_achievement_type_id)` updates `user_levels` and logs `xp_transactions`.

**`process_achievement(code, user_id, metadata?)` algorithm** (triggered server-side, e.g. from `classroom.complete_chapter()` for `lesson_completed`/`course_completed`):
1. Fetch active achievement by code.
2. If not repeatable: skip if already earned (`{skipped: true}`).
3. If repeatable with `daily_limit`: skip if today's count ≥ limit.
4. Insert `user_achievements` row, call `award_xp` RPC.
5. Return `{xp_awarded, new_level, leveled_up, skipped}`.

### Streaks
Daily check-in tracked via RPC `register_daily_login()`. `GET /api/streaks/checkin` registers today's login and returns streak info (including `milestone_reached` when a streak threshold is hit); `GET /api/streaks/me` reads current streak without registering.

### Supabase Storage Buckets
| Bucket | Used for |
|--------|----------|
| `Avatars` | Profile pictures + course thumbnails |
| `Post` | Post images |
| `receipts` | Payment receipt files |
| `chapter-pdfs` | Course chapter PDFs |
| `live-pdfs` | Live session PDFs |
| `level-tier-icons` | Gamification level tier icons |
| `achievement-icons` | Achievement icons |

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

**How token validation works**: each protected request calls `supabase.auth.get_user(token)` with the service-role client to validate the JWT against Supabase Auth. There is no local JWT verification — every call hits Supabase (the Lives WebSocket is the one exception: it validates best-effort and never rejects the connection).

### Permission Matrix

| Action | Dependency | Extra check |
|--------|-----------|-------------|
| Read feed / courses / tags / currencies / payment methods / achievements catalog | `get_optional_user` or public | — |
| Create post / comment / react | `get_current_user` | — |
| Delete post | `get_current_user` | author OR admin |
| Edit post | `get_current_user` | — (currently no author check) |
| Manage courses & chapters (legacy `/api/courses`) | `get_current_user` | Frontend admin-only (no server check) |
| Manage tags | `get_current_user` | Frontend admin-only (no server check) |
| Classroom student progress/completion/chapter PDFs read | `get_active_user` | — |
| Classroom admin (publish, delete chapter, chapter PDFs) | `get_current_admin` | — |
| Lives — read/chat/react/PDFs read | `get_active_user` | — |
| Lives — admin (create/update/activate/delete, moderate chat, manage PDFs) | `get_current_admin` | — |
| Levels/achievements — own data | `get_current_user` | — |
| Levels/achievements/payment-methods/currencies — admin management | `get_current_admin` | — |
| Admin analytics | `get_current_admin` | — |
| All `/api/invitations/` routes | `get_current_admin` | — |
| List / approve / reject payments | `get_current_admin` | — |
| Register + upload receipt | — (public) | Used in onboarding wizard |
| View own payments | `get_current_user` | user_id must match or admin |

> **Gap**: Course and tag management routes (and the legacy `/api/courses` chapter CRUD) use `get_current_user` but the admin check only happens in the frontend. Any authenticated user can technically create/edit/delete courses via the API.

---

## Auth & Registration Flows

### Standard registration (`POST /api/auth/register`)
1. `auth.admin.create_user(email, password, email_confirm=True)` — skips email confirmation
2. Insert `profiles` row (name, role="miembro", avatar="", bio="", subscription_status="inactive")
3. Auto-login via fresh anon client → returns `{user, token}` or `{autoLogin: false}` on failure

### Payment-wizard registration (`POST /api/payments/register`)
1. `auth.admin.create_user(...)` — same as above
2. Insert `profiles` with `subscription_status="inactive"`
3. Insert `payments` with `status="pending"`, `currency_id`, `amount`, `amount_local`, `exchange_rate`, `payment_method_id`
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
  "phone": "string | null",
  "birthdate": "string | null"
}
```
> Keep this shape in sync when adding new profile fields: update `get_me`, `login`, and `update_profile` in `app/services/auth.py` together.

---

## Payment & Subscription Flow

```
User registers via wizard (selects payment_method_id + currency, BCV rate frozen client-side)
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

Auth levels: `—` = public · `🔑` = any authenticated user · `🔓` = active-subscription user (`get_active_user`) · `👑` = admin

### Auth (`/api/auth`)
| Method | Path | Auth | Body / Params | Returns |
|--------|------|------|---------------|---------|
| POST | `/api/auth/register` | — | `{name, email, password, role?}` | `{user, token}` or `{autoLogin: false}` |
| POST | `/api/auth/login` | — | `{email, password}` | `{user, token}` |
| GET | `/api/auth/me` | 🔑 | — | `{user}` |
| POST | `/api/auth/avatar` | 🔑 | `{imageData: "data:image/...;base64,..."}` | `{url}` |
| PUT | `/api/auth/profile` | 🔑 | `{name, avatar, bio, gender?, city?, phone?, birthdate?}` | `{user}` |

### Posts (`/api/posts`)
| Method | Path | Auth | Body / Params | Returns |
|--------|------|------|---------------|---------|
| GET | `/api/posts/` | ? | `?limit=10&cursor=<iso>&tags=t1,t2` | `{posts, nextCursor}` |
| POST | `/api/posts/` | 🔑 | `{content, tagIds?, imageData?}` | Post object |
| GET | `/api/posts/me/social-impact` | 🔑 | — | Aggregate impact stats for current user |
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

### Courses — legacy (`/api/courses`)
Still the primary CRUD path used by the frontend admin classroom UI.

| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| GET | `/api/courses/` | — | — | `[Course]` |
| POST | `/api/courses/` | 🔑 | `{title, description, thumbnail, category?}` | Course |
| POST | `/api/courses/thumbnail` | 🔑 | `{imageData}` | `{url}` |
| PUT | `/api/courses/{course_id}` | 🔑 | `{title?, description?, thumbnail?, category?}` | Course |
| DELETE | `/api/courses/{course_id}` | 🔑 | — | `{deleted: true}` |
| GET | `/api/courses/{course_id}/chapters` | — | — | `[Chapter]` ordered by sort_order |
| POST | `/api/courses/{course_id}/chapters` | 🔑 | `{title, videoUrl?, duration?}` (title ≤150 chars, duration ≤20 chars) | Chapter |
| PUT | `/api/courses/{course_id}/chapters/{chapter_id}` | 🔑 | `{title?, videoUrl?, duration?}` | Chapter |

### Classroom — student-facing (`/api/classroom`, 🔓 unless noted)
| Method | Path | Auth | Returns |
|--------|------|------|---------|
| GET | `/api/classroom/me/completed-courses` | 🔑 | `{completedCourses: number}` |
| GET | `/api/classroom/courses` | 🔓 | `[Course]` |
| GET | `/api/classroom/courses/{course_id}` | 🔓 | Course detail |
| POST | `/api/classroom/courses/{course_id}/chapters/{chapter_id}/complete` | 🔓 | Marks chapter complete, may trigger `lesson_completed`/`course_completed` achievements |
| GET | `/api/classroom/courses/{course_id}/progress` | 🔓 | Progress from `user_course_progress` (not yet consumed by frontend UI) |
| GET | `/api/classroom/chapters/{chapter_id}/pdfs` | 🔓 | `[ChapterPdf]` ordered by sort_order |

### Classroom — admin (`/api/admin/classroom`, 👑)
| Method | Path | Body | Returns |
|--------|------|------|---------|
| POST | `/api/admin/classroom/courses` | `{title, description, thumbnail, category}` | Course (201) |
| PATCH | `/api/admin/classroom/courses/{course_id}` | `{title?, description?, thumbnail?, category?}` | Course |
| PATCH | `/api/admin/classroom/courses/{course_id}/publish` | `{isPublished: bool}` | Course |
| DELETE | `/api/admin/classroom/courses/{course_id}` | — | `{deleted: true}` |
| POST | `/api/admin/classroom/courses/{course_id}/chapters` | `{title, description?, videoUrl?, duration?}` | Chapter (201) |
| PATCH | `/api/admin/classroom/courses/{course_id}/chapters/{chapter_id}` | `{title?, description?, videoUrl?, duration?}` | Chapter |
| DELETE | `/api/admin/classroom/courses/{course_id}/chapters/{chapter_id}` | — | `{deleted: true}` — used by current frontend chapter-delete UI |
| POST | `/api/admin/classroom/chapters/{chapter_id}/pdfs` | `{title, fileData, fileName}` | ChapterPdf (201) — used by current frontend PDF upload |
| PATCH | `/api/admin/classroom/chapters/{chapter_id}/pdfs/{pdf_id}` | `{title?, sortOrder?}` | ChapterPdf |
| DELETE | `/api/admin/classroom/chapters/{chapter_id}/pdfs/{pdf_id}` | — | `{deleted: true}` |

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
| POST | `/api/payments/register` | — | `{name, email, password, plan, amount, amount_local, currency_id, exchange_rate, payment_method_id, reference_number, phone, receipt_path}` | `{user, payment, message}` |
| GET | `/api/payments/` | 👑 | — | `[Payment]` with user_name, ordered newest first |
| GET | `/api/payments/{user_id}` | 🔑 | — | `[Payment]` for that user |
| PATCH | `/api/payments/{id}/approve` | 👑 | — | Payment (sets status=success, expires_at) |
| PATCH | `/api/payments/{id}/reject` | 👑 | — | Payment (sets status=failed) |
| GET | `/api/payments/{id}/receipt` | 👑 | — | `{url, expires_in: 3600}` |

### Payment Methods (`/api/payment-methods`, public read)
| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/payment-methods/` | Active methods with fields/values, ordered by sort_order |
| GET | `/api/payment-methods/{method_id}` | Single method with fields/values |

### Payment Methods — admin (`/api/admin/payment-methods`, 👑)
| Method | Path | Body | Returns |
|--------|------|------|---------|
| GET | `/api/admin/payment-methods/` | — | All methods (active + inactive) |
| POST | `/api/admin/payment-methods/` | `{name, description?, fields?}` | Method (201) |
| PATCH | `/api/admin/payment-methods/{method_id}` | `{name?, description?, is_active?, sort_order?}` | Method |
| PATCH | `/api/admin/payment-methods/{method_id}/toggle` | — | Method |
| DELETE | `/api/admin/payment-methods/{method_id}` | — | `{deleted: true}` (409 if has payments) |
| PUT | `/api/admin/payment-methods/{method_id}/values` | `{values: [...]}` | Upserted values |
| POST | `/api/admin/payment-methods/{method_id}/fields` | `{field_key, field_label, field_type, is_required?, sort_order?}` | Field (201) |
| PATCH | `/api/admin/payment-methods/{method_id}/fields/{field_id}` | `{...}` | Field |
| DELETE | `/api/admin/payment-methods/{method_id}/fields/{field_id}` | — | `{deleted: true}` |

### Currencies (`/api/currencies`, public read)
| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/currencies/` | Active currencies, ordered by code |

### Currencies — admin (`/api/admin/currencies`, 👑)
| Method | Path | Body | Returns |
|--------|------|------|---------|
| GET | `/api/admin/currencies/` | — | All currencies (active + inactive) |
| POST | `/api/admin/currencies/` | `{code, name, symbol}` | Currency (409 if code duplicate) |
| PATCH | `/api/admin/currencies/{currency_id}` | `{code?, name?, symbol?}` | Currency |
| PATCH | `/api/admin/currencies/{currency_id}/toggle` | — | Currency (400 if `is_base`) |
| DELETE | `/api/admin/currencies/{currency_id}` | — | `{deleted: true}` (400 if `is_base`, 409 if has payments) |

### Levels & Achievements (`/api/levels`, `/api/achievements`)
| Method | Path | Auth | Returns |
|--------|------|------|---------|
| GET | `/api/levels/tiers` | — | All tiers ordered by min_level |
| GET | `/api/levels/me` | 🔑 | `{user_id, level, xp_total, xp_current, xp_next, tier?}` |
| GET | `/api/levels/me/achievements` | 🔑 | Earned achievements w/ metadata, newest first |
| GET | `/api/levels/me/xp-history` | 🔑 | XP transactions paginated (`?limit=20&offset=0`) |
| GET | `/api/levels/{user_id}` | — | Another user's level/tier |
| POST | `/api/levels/award` | 🔑 | `{achievement_code, metadata?}` — internal use, call from other services not directly from client |
| GET | `/api/achievements/` | — | Public catalog of active achievements, ordered by xp_reward desc |

### Levels & Achievements — admin (`/api/admin/levels`, `/api/admin/achievements`, 👑)
| Method | Path | Body | Returns |
|--------|------|------|---------|
| GET | `/api/admin/levels/users` | — | All users w/ level/XP, ordered by xp_total desc |
| POST | `/api/admin/levels/award` | `{user_id, xp_amount, reason}` | Result |
| GET | `/api/admin/levels/tiers` | — | All tiers |
| POST | `/api/admin/levels/tiers` | `{name, min_level, max_level, ...}` | Tier (400 if min > max) |
| PATCH | `/api/admin/levels/tiers/{tier_id}` | `{...}` | Tier |
| POST | `/api/admin/levels/tiers/icon` | `{imageData}` | `{url}` (bucket `level-tier-icons`) |
| GET | `/api/admin/achievements/` | — | All achievements incl. inactive |
| POST | `/api/admin/achievements/` | `{code, name, xp_reward, is_repeatable?, daily_limit?, ...}` | Achievement (409 if code duplicate) |
| PATCH | `/api/admin/achievements/{achievement_id}` | `{...}` | Achievement |
| POST | `/api/admin/achievements/icon` | `{imageData}` | `{url}` (bucket `achievement-icons`) |

### Streaks (`/api/streaks`)
| Method | Path | Auth | Returns |
|--------|------|------|---------|
| GET | `/api/streaks/checkin` | 🔑 | Registers today's login via RPC, returns streak + `milestone_reached?` |
| GET | `/api/streaks/me` | 🔑 | Current streak without registering |

### Lives (`/api/lives`, 🔓 unless noted)
| Method | Path | Body / Params | Returns |
|--------|------|----------------|---------|
| GET | `/api/lives/` | — | All sessions, active first then by scheduled_at (cached 5s in Redis) |
| GET | `/api/lives/active` | — | Current active session or null |
| GET | `/api/lives/{live_id}/chat` | `?limit=50(max 100)&after=<cursor>` | Messages |
| POST | `/api/lives/{live_id}/chat` | `{content}` | Message (201), broadcast via WS |
| GET | `/api/lives/{live_id}/reactions` | — | Aggregated reactions + own reaction |
| POST | `/api/lives/{live_id}/react` | `{reactionType}` | Toggle (same type removes, different replaces), broadcasts `reaction_update` |
| GET | `/api/lives/{live_id}/pdfs` | — | PDFs ordered by sort_order |
| WS | `/api/lives/{live_id}/chat/ws?token=<jwt>` | — | Real-time chat broadcast channel |

### Lives — admin (`/api/admin/lives`, 👑)
| Method | Path | Body | Returns |
|--------|------|------|---------|
| POST | `/api/admin/lives/` | `{title, description?, youtubeUrl?, scheduledAt?}` | Live (201) |
| PATCH | `/api/admin/lives/{live_id}` | `{...}` | Updated live |
| PATCH | `/api/admin/lives/{live_id}/activate` | `{isActive: bool}` | Updated live (deactivates any other active live first) |
| DELETE | `/api/admin/lives/{live_id}` | — | `{deleted: true}` |
| POST | `/api/admin/lives/{live_id}/pdfs` | `{title, fileData, filename}` | PDF (201, bucket `live-pdfs`) |
| DELETE | `/api/admin/lives/{live_id}/pdfs/{pdf_id}` | — | `{deleted: true}` |
| PATCH | `/api/admin/lives/{live_id}/chat/{message_id}` | `{content}` | Updated message, broadcasts `edit_message` |
| DELETE | `/api/admin/lives/{live_id}/chat/{message_id}` | — | `{deleted: true, id}`, broadcasts `delete_message` |
| POST | `/api/admin/lives/{live_id}/chat/{message_id}/pin` | `{isPinned: bool}` | Updated message, broadcasts `pin_message` |

### Admin Analytics (`/api/admin/analytics`, 👑)
| Method | Path | Params | Returns |
|--------|------|--------|---------|
| GET | `/api/admin/analytics/overview` | — | Real-time members + revenue summary |
| GET | `/api/admin/analytics/members` | — | Member totals, gender, city, age-range breakdown |
| GET | `/api/admin/analytics/revenue` | — | Real-time revenue detail |
| GET | `/api/admin/analytics/history` | `?from_date&to_date&limit=30(max 365)` | Daily snapshots, newest first |
| POST | `/api/admin/analytics/snapshot` | — | Forces today's snapshot generation/refresh |

Reads Supabase views `v_stats_members`, `v_stats_revenue`, `v_analytics_history`. Results are cached in Redis with short TTLs (30–300s) to amortize parallel admin-panel requests.

### Health
| Method | Path | Auth | Returns |
|--------|------|------|---------|
| GET | `/` | — | `{status: "ok", supabase: bool, redis: bool}` |

---

## Key Business Rules

- **Subscription gating**: only `miembro` role is gated. `admin` and `invitado` always have full access.
- **One reaction per user**: unique constraint on `(post_id, user_id)`, `(comment_id, user_id)`, and `(live_id, user_id)`.
- **Post delete permission**: user must be author OR admin. Route checks this after fetching the post.
- **Tag deduplication**: stored as lowercase; `create_tag` returns the existing tag if name already exists.
- **Chapter sort_order**: auto-calculated as `existing_count` at insert time. No reorder endpoint.
- **Receipt / PDF path sanitization**: chars outside `[a-zA-Z0-9._-]` replaced with `_` to prevent path traversal — applied consistently across receipts, chapter PDFs, and live PDFs.
- **Invitation single-use**: `used_at` is set by the RPC `use_invitation(token)` on successful registration.
- **No transaction support**: multi-step operations (register + insert profile + insert payment) use try/except with manual rollback. A partial failure may leave orphaned auth users — check Supabase Auth dashboard if registration seems broken.
- **Module label sync**: always call `_sync_module_label(supabase, course_id)` after inserting/updating/deleting chapters (legacy `courses.py` path).
- **Currency `is_base` is protected**: the base currency (USD) can't be deactivated or deleted; other currencies can't be deleted if they have associated payments.
- **Live activation is exclusive**: activating one live session deactivates any other currently-active session.
- **Achievement idempotency**: non-repeatable achievements silently skip if already earned; repeatable ones respect `daily_limit`.
- **Frozen exchange rate**: `payments.exchange_rate` and `amount_local` are snapshots taken at registration time — never recalculate retroactively even if the BCV rate changes later.

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
[lives.ws] conectado live_id=<id> user=<id>
```

Pattern: `[<module>.<function>] <context>`. Use `logger.info` for happy path, `logger.warning` for recoverable issues, `logger.error(..., exc_info=True)` for failures.

---

## Known Gotchas

1. **Trailing slash required on collection endpoints** — routes are defined as `@router.get("/")` under a prefix, making full paths like `/api/posts/`. Missing the slash causes a 307 redirect, and the browser strips the `Authorization` header on cross-origin redirects (confirmed bug).

2. **Service-role client session contamination** — never call `sign_in_with_password` on `get_supabase()`. Use `create_anon_client()` instead. Mixing auth operations into the singleton corrupts its session state for subsequent DB calls.

3. **Supabase token validation is remote** — `supabase.auth.get_user(token)` makes an HTTP call to Supabase on every authenticated request. There is no local JWT caching. High request rates will hit Supabase Auth rate limits. The Lives WebSocket is the one place this validation is best-effort (failure doesn't reject the connection, since the socket is read-broadcast only).

4. **`posts_view` is the only safe way to paginate** — the cursor uses `created_at`, which must come from the view to include the pinning-aware ordering.

5. **`created_by` on courses is optional** — the column may not exist in all environments. `create_course` retries without it if the first insert fails.

6. **Signed receipt URLs expire in 1 hour** — admin must re-request `GET /api/payments/{id}/receipt` if the URL was cached.

7. **RPC return shape** — `validate_invitation` and other RPCs may return a list `[{...}]` or a dict `{...}`. The `_normalize_rpc()` helper in invitations service handles both.

8. **`subscription_status` NOT NULL constraint** — the `sync_subscription_status` trigger must handle ALL payment status values (`pending`, `success`, `failed`). If a new status is added without updating the trigger, INSERT will fail with a NOT NULL violation.

9. **Two parallel course CRUD surfaces** — `app/api/courses.py` (legacy, mounted at `/api/courses`) is still the path the frontend admin UI uses to create/edit/delete courses and chapters. `app/api/admin_classroom.py` (mounted at `/api/admin/classroom`) duplicates chapter delete and owns chapter PDF management and course publish toggling. Don't assume one supersedes the other — check which one the frontend component actually calls before changing behavior.

10. **`/api/classroom` progress-tracking endpoints exist but aren't fully wired into the UI** — `complete_chapter` / `get_course_progress` / `get_completed_courses_count` are implemented and used by `Profile.tsx` for the completed-courses count and by the achievement triggers, but `CourseDetail.tsx` on the frontend still reads the static `courses.progress` column rather than calling `get_course_progress`.
