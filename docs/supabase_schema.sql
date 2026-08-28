-- Supabase SQL Editor에서 실행할 스키마.
--
-- 사전에 대시보드에서 Storage 버킷 2개를 수동 생성해야 한다 (SQL로는 생성 불가):
--   violations          -> Public.  얼굴 블러 + 바운딩 박스가 그려진 공개용 이미지
--   violations-original  -> Private. 블러 없는 원본. 절대 Public으로 두지 말 것
--
-- 원본은 URL이 아니라 경로(image_path_original)만 DB에 저장한다. 열람이 필요하면
-- service_role 키로 signed URL을 발급하는 방식으로 나중에 붙인다.

create table if not exists violations (
    id uuid primary key default gen_random_uuid(),
    device_id text not null,
    "timestamp" timestamptz not null default now(),
    image_url text not null,              -- 블러 처리된 공개용 이미지 URL
    image_path_original text,             -- 비공개 버킷 내 원본 경로 (URL 아님)
    class_label text not null,            -- NO-Hardhat / NO-Mask / NO-Safety Vest
    confidence real not null,
    bbox jsonb,                           -- [x1, y1, x2, y2]
    reviewed boolean not null default false,
    created_at timestamptz not null default now()
);

create index if not exists idx_violations_device_id on violations (device_id);
create index if not exists idx_violations_timestamp on violations ("timestamp" desc);
create index if not exists idx_violations_class_label on violations (class_label);

alter table violations enable row level security;

-- anon 키(웹 대시보드)는 읽기만 가능. image_path_original 컬럼이 노출되지만
-- 비공개 버킷이라 경로만으로는 접근 불가.
create policy "allow read for anon" on violations
    for select
    using (true);

-- 프로젝트 설정에서 "Automatically expose new tables"를 끈 상태이므로
-- 각 역할에 테이블 권한을 명시적으로 준다. service_role은 RLS는 우회하지만
-- 테이블 GRANT까지 우회하지는 않으므로 반드시 따로 줘야 한다.
grant select on violations to anon, authenticated;
grant select, insert, update on violations to service_role;

-- 검토 처리(오탐 표시)를 위해 anon에게 reviewed 컬럼만 UPDATE를 허용한다.
-- web 컨테이너에 service_role 키를 통째로 주면 대시보드 취약점 하나가
-- DB 전권 탈취로 이어지므로, 컬럼 단위로 최소 권한만 준다.
grant update (reviewed) on violations to anon;

create policy "allow update reviewed for anon" on violations
    for update
    using (true)
    with check (true);

-- insert/update는 service_role 키(inference 컨테이너)만 사용하며 RLS를 우회하므로
-- 별도 정책이 필요 없다.
