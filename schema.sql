-- ============ TABLES ============
create table if not exists users (
  id bigint primary key,
  username text,
  first_name text,
  balance numeric default 0 not null,
  is_admin boolean default false,
  created_at timestamptz default now()
);

create table if not exists products (
  id bigserial primary key,
  name text not null,
  category text not null,
  price numeric not null,
  stock int default 0 not null,
  description text default '',
  is_active boolean default true,
  created_at timestamptz default now()
);

create table if not exists product_items (
  id bigserial primary key,
  product_id bigint references products(id) on delete cascade,
  content text not null,
  is_sold boolean default false,
  sold_to bigint,
  sold_at timestamptz
);

create index if not exists idx_items_avail
  on product_items (product_id, is_sold) where is_sold = false;

create table if not exists orders (
  id bigserial primary key,
  user_id bigint references users(id),
  product_id bigint references products(id),
  quantity int default 1,
  total_price numeric,
  status text default 'completed',
  delivered_items text,
  created_at timestamptz default now()
);

create table if not exists transactions (
  id bigserial primary key,
  user_id bigint references users(id),
  type text not null,
  amount numeric not null,
  balance_after numeric,
  reference text,
  note text,
  created_at timestamptz default now()
);

create index if not exists idx_tx_user
  on transactions (user_id, created_at desc);

create table if not exists topup_requests (
  id bigserial primary key,
  user_id bigint references users(id),
  amount numeric not null,
  txn_id text,
  status text default 'pending',
  created_at timestamptz default now()
);

-- ============ RPC: purchase_product ============
create or replace function purchase_product(p_user_id bigint, p_product_id bigint)
returns json
language plpgsql
as $$
declare
  v_price numeric;
  v_balance numeric;
  v_item_id bigint;
  v_item_content text;
  v_order_id bigint;
  v_new_balance numeric;
begin
  select price into v_price from products where id = p_product_id and is_active = true;
  if v_price is null then raise exception 'product_not_found'; end if;

  select balance into v_balance from users where id = p_user_id for update;
  if v_balance is null then raise exception 'user_not_found'; end if;
  if v_balance < v_price then raise exception 'insufficient_balance'; end if;

  select id, content into v_item_id, v_item_content
    from product_items
    where product_id = p_product_id and is_sold = false
    order by id
    limit 1
    for update skip locked;

  if v_item_id is null then raise exception 'out_of_stock'; end if;

  update users set balance = balance - v_price where id = p_user_id
    returning balance into v_new_balance;
  update product_items set is_sold = true, sold_to = p_user_id, sold_at = now() where id = v_item_id;
  update products set stock = greatest(stock - 1, 0) where id = p_product_id;

  insert into orders (user_id, product_id, quantity, total_price, status, delivered_items)
    values (p_user_id, p_product_id, 1, v_price, 'completed', v_item_content)
    returning id into v_order_id;

  insert into transactions (user_id, type, amount, balance_after, reference, note)
    values (p_user_id, 'purchase', -v_price, v_new_balance, v_order_id::text, 'Product purchase');

  return json_build_object(
    'order_id', v_order_id,
    'content', v_item_content,
    'new_balance', v_new_balance
  );
end;
$$;

-- ============ RPC: approve_topup ============
create or replace function approve_topup(p_topup_id bigint)
returns void
language plpgsql
as $$
declare
  v_user_id bigint;
  v_amount numeric;
  v_status text;
  v_new_balance numeric;
begin
  select user_id, amount, status into v_user_id, v_amount, v_status
    from topup_requests where id = p_topup_id for update;

  if v_status is null then raise exception 'not_found'; end if;
  if v_status <> 'pending' then raise exception 'already_processed'; end if;

  update users set balance = balance + v_amount where id = v_user_id
    returning balance into v_new_balance;

  update topup_requests set status = 'approved' where id = p_topup_id;

  insert into transactions (user_id, type, amount, balance_after, reference, note)
    values (v_user_id, 'topup', v_amount, v_new_balance, p_topup_id::text, 'Topup approved');
end;
$$;
