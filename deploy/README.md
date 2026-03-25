# P4GitSync 배포 가이드

## 빠른 시작 (Docker)

### 1. 설정 수정

```bash
# config.toml — P4 서버 주소, stream, workspace 등 수정
# user_mapper.py — workspace 패턴, 이메일 도메인 수정
```

### 2. 빌드 & 실행

```bash
cd deploy
docker compose build
docker compose up -d
```

### 3. 상태 확인

```bash
curl http://localhost:8080/api/health
docker compose logs -f p4gitsync
```

### 4. 초기 히스토리 import

```bash
docker compose exec p4gitsync p4gitsync import --stream //YourDepot/main
```

### 5. 중지

```bash
docker compose down
```

## 파일 구조

```
deploy/
  docker-compose.yml  — Docker 서비스 정의
  config.toml         — P4GitSync 설정
  user_mapper.py      — 사용자 매핑 플러그인 (공유 계정 환경용)
```

## pip 설치 (Docker 없이)

```bash
cd ../p4gitsync
pip install -e .
p4gitsync --config ../deploy/config.toml run
```

## 설정 커스터마이즈

- `config.toml`: 02-Configuration.md 참조
- `user_mapper.py`: 조직의 P4 계정/workspace 규칙에 맞게 수정
