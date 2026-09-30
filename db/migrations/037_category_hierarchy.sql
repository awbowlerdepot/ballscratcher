-- 037_category_hierarchy.sql
--
-- Nested Learn-site categories. Al (2026-09-30): "create a category
-- 'Bowling Tips' and then that can have sub categories and then we can
-- add videos from youtube there" -- picked true nested categories (any
-- depth, e.g. Bowling Tips > Spare Shooting > ...) over reusing
-- article_types as a fixed second level.
--
-- categories.parent_id is a self-reference: null = top-level (what the
-- Learn header's tab row shows), set = a subcategory. article_types stay
-- as they are -- "what kind of write-up" (Ball Review, a future video
-- tip) is a separate question from "where in the tree it lives", so
-- they're orthogonal to this hierarchy rather than replaced by it.
--
-- on delete restrict: deleting a category that still has subcategories
-- fails at the DB level instead of silently orphaning or cascading a
-- subtree away. admin_api's delete_category checks first and returns a
-- clear 409; this is the backstop. Cycles (a category becoming its own
-- ancestor) are prevented in admin_api.update_category -- the check
-- constraint below only catches the trivial self-parent case.

begin;

alter table categories
    add column parent_id uuid references categories(id) on delete restrict;

alter table categories
    add constraint categories_parent_not_self check (parent_id is null or parent_id <> id);

create index idx_categories_parent_id on categories(parent_id);

comment on column categories.parent_id is 'Parent category (migration 037) -- null for a top-level category (a Learn header tab), set for a subcategory. Any depth. on delete restrict: remove or move subcategories before deleting their parent.';

commit;
